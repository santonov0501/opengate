from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
DEFAULT_SUBSCRIPTION_ID = "default"
KEYS_JSON_PATH = DATA_DIR / "keys.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    with connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'active',
                title TEXT NOT NULL DEFAULT 'OpenGate',
                settings_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                photo_url TEXT NOT NULL DEFAULT '',
                access_status TEXT NOT NULL DEFAULT 'active',
                subscription_id TEXT NOT NULL DEFAULT 'default',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT,
                FOREIGN KEY (subscription_id) REFERENCES subscriptions(id)
            );

            CREATE TABLE IF NOT EXISTS keys (
                id TEXT PRIMARY KEY,
                uri TEXT NOT NULL UNIQUE,
                region TEXT NOT NULL DEFAULT '',
                region_name TEXT NOT NULL DEFAULT '',
                host TEXT,
                port INTEGER,
                latency_ms REAL,
                first_seen_at TEXT,
                last_seen_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'pull_keys',
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS key_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT NOT NULL,
                pulled_at TEXT NOT NULL,
                raw_updated_at TEXT,
                total_keys INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS encrypted_links (
                user_id INTEGER PRIMARY KEY,
                encrypted_link TEXT,
                expires_at TEXT,
                updated_at TEXT
            );
            """
        )
        now = utc_now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO subscriptions (id, status, title, settings_json, created_at, updated_at)
            VALUES (?, 'active', 'OpenGate', ?, ?, ?)
            """,
            (DEFAULT_SUBSCRIPTION_ID, json.dumps(default_subscription_settings()), now, now),
        )


def parse_subscription_params() -> dict[str, str]:
    raw = os.getenv("SUBSCRIPTION_PARAMS", "").strip()
    if not raw:
        return {}
    params: dict[str, str] = {}
    for item in re.split(r"[;,]\s*", raw):
        if not item:
            continue
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key:
            params[key] = value
    return params


def default_subscription_settings() -> dict[str, Any]:
    settings = parse_subscription_params()
    provider_id = os.getenv("PROVIDER_ID", "").strip()
    if provider_id and "providerid" not in settings:
        settings["providerid"] = provider_id
    return settings


def upsert_user(user: dict[str, Any], access_status: str = "active") -> dict[str, Any]:
    init_db()
    now = utc_now_iso()
    user_id = int(user["id"])
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (
                id, first_name, last_name, username, photo_url, access_status,
                subscription_id, created_at, updated_at, last_login_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                username = excluded.username,
                photo_url = excluded.photo_url,
                updated_at = excluded.updated_at,
                last_login_at = excluded.last_login_at
            """,
            (
                user_id,
                user.get("first_name", ""),
                user.get("last_name", ""),
                user.get("username", ""),
                user.get("photo_url", ""),
                access_status,
                DEFAULT_SUBSCRIPTION_ID,
                now,
                now,
                now,
            ),
        )
        return get_user(user_id, conn=conn) or {"id": user_id}


def get_user(user_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
    owns_conn = conn is None
    if conn is None:
        init_db()
        conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            conn.close()


def user_has_access(user_id: int) -> bool:
    user = get_user(user_id)
    if not user:
        return False
    if user["access_status"] != "active":
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM subscriptions WHERE id = ?",
            (user["subscription_id"],),
        ).fetchone()
    return bool(row and row["status"] == "active")


def normalize_key_entries(raw_data: dict[str, Any], region_names: dict[str, str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for region, region_data in raw_data.items():
        if not isinstance(region_data, dict):
            continue
        region_name = region_names.get(region, region)
        for item in region_data.get("top10", []):
            if not isinstance(item, dict):
                continue
            uri = (item.get("key") or "").strip()
            if not uri:
                continue
            entries.append(
                {
                    "uri": uri,
                    "region": region,
                    "region_name": region_name,
                    "host": item.get("host"),
                    "port": item.get("port"),
                    "latency_ms": item.get("latency_ms"),
                    "first_seen_at": item.get("first_seen"),
                    "raw_json": json.dumps(item, ensure_ascii=False),
                }
            )

        best = region_data.get("best")
        if isinstance(best, str) and best.strip():
            entries.append(
                {
                    "uri": best.strip(),
                    "region": region,
                    "region_name": region_name,
                    "host": None,
                    "port": None,
                    "latency_ms": None,
                    "first_seen_at": None,
                    "raw_json": None,
                }
            )

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        if entry["uri"] in seen:
            continue
        seen.add(entry["uri"])
        deduped.append(entry)
    return deduped


def replace_keys(
    entries: list[dict[str, Any]],
    source_url: str,
    raw_updated_at: str | None = None,
    db_path: Path = DB_PATH,
) -> None:
    # Write keys to a JSON file (primary storage for keys)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = utc_now_iso()
    json_entries: list[dict[str, Any]] = []
    for entry in entries:
        json_entries.append(
            {
                "uri": entry.get("uri"),
                "region": entry.get("region") or "",
                "region_name": entry.get("region_name") or "",
                "host": entry.get("host"),
                "port": entry.get("port"),
                "latency_ms": entry.get("latency_ms"),
                "first_seen_at": entry.get("first_seen_at"),
                "raw_json": entry.get("raw_json"),
                "last_seen_at": now,
                "source": "pull_keys",
            }
        )
    with open(KEYS_JSON_PATH, "w", encoding="utf-8") as fh:
        json.dump(json_entries, fh, ensure_ascii=False, indent=2)

    # Record the update in key_updates table for history/metadata
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO key_updates (source_url, pulled_at, raw_updated_at, total_keys, status)
            VALUES (?, ?, ?, ?, 'ok')
            """,
            (source_url, now, raw_updated_at, len(entries)),
        )


def record_key_update_error(source_url: str, error: str, db_path: Path = DB_PATH) -> None:
    init_db(db_path)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO key_updates (source_url, pulled_at, total_keys, status, error)
            VALUES (?, ?, 0, 'error', ?)
            """,
            (source_url, utc_now_iso(), error),
        )


def list_key_rows() -> list[dict[str, Any]]:
    # Prefer JSON file storage if present, fall back to DB keys table for compatibility
    if KEYS_JSON_PATH.exists():
        try:
            with open(KEYS_JSON_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                rows: list[dict[str, Any]] = []
                for item in data:
                    rows.append(
                        {
                            "uri": item.get("uri"),
                            "region": item.get("region") or "",
                            "region_name": item.get("region_name") or "",
                            "host": item.get("host"),
                            "port": item.get("port"),
                            "latency_ms": item.get("latency_ms"),
                        }
                    )
                return rows
        except Exception:
            # If JSON read fails, fall back to DB
            pass

    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT uri, region, region_name, host, port, latency_ms
            FROM keys
            ORDER BY rowid
            """
        ).fetchall()
    return [dict(row) for row in rows]


def latest_key_update() -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM key_updates ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def upsert_encrypted_link(user_id: int, encrypted_link: str, expires_at: str) -> None:
    init_db()
    now = utc_now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO encrypted_links (user_id, encrypted_link, expires_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                encrypted_link = excluded.encrypted_link,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (user_id, encrypted_link, expires_at, now),
        )


def get_encrypted_link(user_id: int) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT encrypted_link, expires_at, updated_at FROM encrypted_links WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def list_active_user_ids() -> list[int]:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM users WHERE access_status = 'active'"
        ).fetchall()
    return [int(r[0]) for r in rows]


def subscription_headers(user_id: int) -> dict[str, str]:
    user = get_user(user_id)
    title = "OpenGate"
    if user:
        with connect() as conn:
            row = conn.execute(
                "SELECT title, settings_json FROM subscriptions WHERE id = ?",
                (user["subscription_id"],),
            ).fetchone()
        if row:
            title = row["title"]
            settings = {**default_subscription_settings(), **json.loads(row["settings_json"] or "{}")}  # ensure required defaults are present
        else:
            settings = default_subscription_settings()
    else:
        settings = default_subscription_settings()

    headers = {
        "profile-title": f"{title} {user_id}"[:25],
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    headers.update({str(k): str(v) for k, v in settings.items()})
    return headers


def build_subscription_text(user_id: int) -> str:
    headers = subscription_headers(user_id)
    keys = [row["uri"] for row in list_key_rows()]
    if not keys:
        return ""
    lines = []
    for key in sorted(headers):
        value = headers[key]
        if key == "providerid":
            lines.append(f"#{key} {value}")
        else:
            lines.append(f"#{key}: {value}")
    lines.extend(keys)
    return "\n".join([*lines, ""])


def build_subscription_json(user_id: int) -> dict[str, Any]:
    headers = subscription_headers(user_id)
    servers = []
    for row in list_key_rows():
        servers.append(
            {
                "uri": row["uri"],
                "remarks": row["region_name"] or row["region"],
                "meta": {
                    "serverDescription": row["region_name"] or row["region"] or "VLESS",
                },
            }
        )
    return {
        "subscription": {
            "id": DEFAULT_SUBSCRIPTION_ID,
            "status": "active",
            "params": headers,
        },
        "servers": servers,
    }
