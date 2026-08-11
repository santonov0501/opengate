from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import storage


BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
DATA_DIR = PROJECT_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
PYTHON = sys.executable
MAX_LOG_LINES = 300
PULL_KEYS_TIMEOUT = 120  # seconds
SUBSCRIPTION_EXPIRY_CHECK_INTERVAL = 30  # seconds

load_dotenv(PROJECT_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS: set[int] = {
    int(x) for x in os.getenv("TELEGRAM_ALLOWED_IDS", "").split(",") if x.strip().isdigit()
}
SESSION_TTL = timedelta(hours=int(os.getenv("SESSION_TTL_HOURS", "24")))
SUBSCRIPTION_LINK_TTL = int(os.getenv("SUBSCRIPTION_LINK_TTL_SECONDS", "86400"))
INIT_DATA_MAX_AGE = 86400  # initData считается валидным не более 24 часов

# Optional public base URL used to precompute signed URLs for Happ crypto API.
# Example: https://example.com/
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()
PROVIDER_ID = os.getenv("PROVIDER_ID", "").strip()

# Секрет для подписи URL файлов подписки (для Happ).
# По умолчанию генерируется при старте; чтобы ссылки оставались валидными
# после перезапуска, можно задать переменную окружения SIGNING_KEY_HEX.
signing_key_hex = os.getenv("SIGNING_KEY_HEX", "").strip()
if signing_key_hex:
    try:
        SIGNING_KEY = bytes.fromhex(signing_key_hex)
    except Exception:
        SIGNING_KEY = secrets.token_bytes(32)
else:
    SIGNING_KEY = secrets.token_bytes(32)

# Сессии: token -> (user_id, expires_at)
sessions: dict[str, tuple[int, datetime]] = {}
sessions_lock = threading.Lock()

# Rate limiting для /auth/login: ip -> deque(timestamps)
LOGIN_RATE_LIMIT = int(os.getenv("LOGIN_RATE_LIMIT", "10"))  # max attempts
LOGIN_RATE_WINDOW = int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "60"))  # per window
login_attempts: dict[str, deque[float]] = defaultdict(deque)
login_attempts_lock = threading.Lock()

# Кэш encrypted links: user_id -> (encrypted_link, expires_at)
ENCRYPTED_LINK_CACHE_TTL = int(os.getenv("ENCRYPTED_LINK_CACHE_TTL_SECONDS", "300"))  # 5 min
encrypted_link_cache: dict[int, tuple[str, int]] = {}
encrypted_link_cache_lock = threading.Lock()

# Максимум параллельных вызовов crypto API
CRYPTO_API_CONCURRENCY = int(os.getenv("CRYPTO_API_CONCURRENCY", "5"))


def get_cached_encrypted_link(user_id: int) -> str | None:
    """Возвращает закэшированную encrypted link, если она не истекла."""
    with encrypted_link_cache_lock:
        cached = encrypted_link_cache.get(user_id)
        if cached:
            link, expires_at = cached
            if expires_at > int(time.time()):
                return link
            del encrypted_link_cache[user_id]
    return None


def set_cached_encrypted_link(user_id: int, link: str, expires_at: int) -> None:
    """Сохраняет encrypted link в кэше с TTL."""
    with encrypted_link_cache_lock:
        encrypted_link_cache[user_id] = (link, expires_at)


def check_login_rate_limit(ip: str) -> None:
    """Проверяет лимит попыток входа для IP-адреса.

    Использует sliding window: удаляет попытки старше окна,
    затем проверяет количество попыток в текущем окне.
    """
    now = time.time()
    with login_attempts_lock:
        attempts = login_attempts[ip]
        # Удаляем устаревшие попытки
        while attempts and now - attempts[0] > LOGIN_RATE_WINDOW:
            attempts.popleft()
        if len(attempts) >= LOGIN_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Please try again later.",
            )
        attempts.append(now)


class CommandResult(BaseModel):
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class ManagedScriptStatus(BaseModel):
    running: bool
    pid: int | None = None
    started_at: datetime | None = None
    expires_at: datetime | None = None
    public_url: str | None = None
    ios_deep_link: str | None = None
    android_deep_link: str | None = None
    encrypted_link: str | None = None
    returncode: int | None = None
    command: list[str] | None = None
    logs: list[str] = Field(default_factory=list)


class BuildSubscriptionRequest(BaseModel):
    active_minutes: int = Field(default=5, ge=1, le=60)
    port: int = Field(default=8000, ge=1, le=65535)
    name: str | None = None
    slug: str | None = None
    profile_title: str | None = None
    force_restart: bool = False


class KeysStatus(BaseModel):
    path: str
    exists: bool
    updated_at: datetime | None = None
    size_bytes: int | None = None
    total_keys: int | None = None


class LoginRequest(BaseModel):
    init_data: str


class AuthUser(BaseModel):
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    photo_url: str = ""


class LoginResponse(BaseModel):
    token: str
    user: AuthUser
    expires_at: datetime


class ManagedProcess:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.command: list[str] | None = None
        self.started_at: datetime | None = None
        self.expires_at: datetime | None = None
        self.logs: list[str] = []
        self.public_url: str | None = None
        self.ios_deep_link: str | None = None
        self.android_deep_link: str | None = None
        self.encrypted_link: str | None = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def status(self) -> ManagedScriptStatus:
        proc = self.process
        return ManagedScriptStatus(
            running=self.is_running(),
            pid=proc.pid if proc else None,
            started_at=self.started_at,
            expires_at=self.expires_at,
            public_url=self.public_url,
            ios_deep_link=self.ios_deep_link,
            android_deep_link=self.android_deep_link,
            encrypted_link=self.encrypted_link,
            returncode=proc.poll() if proc else None,
            command=self.command,
            logs=self.logs[-MAX_LOG_LINES:],
        )

    def start(self, command: list[str], ttl_seconds: int, cwd: Path = PROJECT_DIR) -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.logs = []
        self.public_url = None
        self.ios_deep_link = None
        self.android_deep_link = None
        self.encrypted_link = None
        self.command = command
        self.started_at = datetime.now(timezone.utc)
        self.expires_at = self.started_at + timedelta(seconds=ttl_seconds)
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        proc = self.process
        if not proc or not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip()
            match = re.search(r"Public HTTPS URL \(ngrok\):\s+(https://[^\s]+)", line)
            if match:
                self.public_url = match.group(1)
            ios_match = re.search(r"iOS deep link:\s+(\S+)", line)
            if ios_match:
                self.ios_deep_link = ios_match.group(1)
            android_match = re.search(r"Android deep link:\s+(\S+)", line)
            if android_match:
                self.android_deep_link = android_match.group(1)
            encrypted_match = re.search(r"Encrypted subscription link .*:\s+(happ://\S+)", line)
            if encrypted_match:
                self.encrypted_link = encrypted_match.group(1)
            self.logs.append(line)
            if len(self.logs) > MAX_LOG_LINES:
                self.logs = self.logs[-MAX_LOG_LINES:]

    def terminate(self) -> None:
        proc = self.process
        if not proc or proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
            return
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)


build_subscription_process = ManagedProcess()
build_lock = asyncio.Lock()
pull_lock = asyncio.Lock()
scheduler_task: asyncio.Task[None] | None = None
expiry_task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------------------
# Авторизация через Telegram Mini Apps
# ---------------------------------------------------------------------------

def verify_init_data(init_data: str) -> dict:
    """Проверяет подпись initData из Telegram Mini Apps.

    Подпись гарантирует, что данные (включая user.id) действительно
    сформированы Telegram и не были подменены клиентом.
    """
    if not TELEGRAM_BOT_TOKEN:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not configured")

    parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(parsed)
    received_hash = data.pop("hash", "")
    if not received_hash:
        raise HTTPException(status_code=401, detail="Missing initData hash")

    # data_check_string: отсортированные по алфавиту key=value, разделённые \n
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))

    # secret_key = HMAC_SHA256(bot_token, "WebAppData")
    secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
    # hash = HMAC_SHA256(data_check_string, secret_key)
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise HTTPException(status_code=401, detail="Invalid initData signature")

    # Проверка свежести initData
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > INIT_DATA_MAX_AGE:
        raise HTTPException(status_code=401, detail="initData is too old")

    # user.id берётся ТОЛЬКО из проверенного initData
    try:
        user = json.loads(data.get("user", "{}"))
        user_id = int(user.get("id", 0))
    except (ValueError, TypeError):
        user_id = 0
    if not user_id:
        raise HTTPException(status_code=401, detail="User not found in initData")

    # Ограничение по списку разрешённых ID (если задан)
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        raise HTTPException(status_code=403, detail="Access denied for this user")

    return {
        "id": user_id,
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", ""),
        "username": user.get("username", ""),
        "photo_url": user.get("photo_url", ""),
    }


def create_session(user_id: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + SESSION_TTL
    with sessions_lock:
        sessions[token] = (user_id, expires_at)
    return token, expires_at


def get_current_user(request: Request) -> dict:
    """FastAPI-зависимость: требует валидную сессию.

    user_id берётся из сессии, созданной при проверке initData.
    Никакие параметры запроса (path/query/body) не влияют на identity.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    with sessions_lock:
        session = sessions.get(token)
        if not session:
            raise HTTPException(status_code=401, detail="Invalid or expired session")
        user_id, expires_at = session
        if datetime.now(timezone.utc) > expires_at:
            del sessions[token]
            raise HTTPException(status_code=401, detail="Session expired")
        db_user = storage.get_user(user_id)
        if not db_user or not storage.user_has_access(user_id):
            raise HTTPException(status_code=403, detail="Access denied for this user")
        return db_user


def make_signed_subscription_url(request: Request, filename: str, user_id: int) -> str:
    """Создаёт подписанный URL на файл подписки для Happ.

    Happ скачивает файл по этому URL без заголовков авторизации,
    но URL действителен ограниченное время и подписан секретом.
    user_id зашит в подпись, чтобы сервер знал, кому отдаётся файл.
    """
    expires = int(time.time()) + SUBSCRIPTION_LINK_TTL
    payload = f"{filename}:{user_id}:{expires}"
    sig = hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    # Use PUBLIC_BASE_URL if configured to ensure HTTPS scheme
    base_url = PUBLIC_BASE_URL if PUBLIC_BASE_URL else str(request.base_url)
    target_url = f"{base_url}{filename}?expires={expires}&uid={user_id}&sig={sig}"
    if PROVIDER_ID:
        target_url = f"{target_url}#?providerid={urllib.parse.quote(PROVIDER_ID)}"
    return target_url


def verify_signed_file(request: Request, filename: str) -> int | None:
    """Проверяет подпись URL файла подписки.

    Возвращает user_id из подписи или None, если подпись невалидна/просрочена.
    """
    expires = request.query_params.get("expires", "")
    uid = request.query_params.get("uid", "")
    sig = request.query_params.get("sig", "")
    if not expires or not uid or not sig:
        return None
    try:
        if int(expires) < time.time():
            return None
        user_id = int(uid)
    except ValueError:
        return None
    payload = f"{filename}:{user_id}:{expires}"
    expected = hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return user_id


def make_signed_redirect_url(request: Request, user_id: int) -> str:
    """Создаёт подписанный URL на /subscription-redirect-302.

    Фронт открывает этот URL через tg.openLink() в системном браузере,
    где нет доступа к Bearer-токену. Подпись и есть авторизация.
    user_id зашит в подпись, чтобы redirect знал, для кого шифровать подписку.
    """
    expires = int(time.time()) + SUBSCRIPTION_LINK_TTL
    payload = f"redirect:{user_id}:{expires}"
    sig = hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    # Use PUBLIC_BASE_URL if configured to ensure HTTPS scheme
    base_url = PUBLIC_BASE_URL if PUBLIC_BASE_URL else str(request.base_url)
    return f"{base_url}subscription-redirect-302?expires={expires}&uid={user_id}&sig={sig}"


def verify_signed_redirect(request: Request) -> int | None:
    """Проверяет подпись URL /subscription-redirect.

    Возвращает user_id из подписи или None, если подпись невалидна/просрочена.
    """
    expires = request.query_params.get("expires", "")
    uid = request.query_params.get("uid", "")
    sig = request.query_params.get("sig", "")
    if not expires or not uid or not sig:
        return None
    try:
        if int(expires) < time.time():
            return None
        user_id = int(uid)
    except ValueError:
        return None
    payload = f"redirect:{user_id}:{expires}"
    expected = hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return user_id


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------

def next_half_hour(now: datetime) -> datetime:
    minute = 30 if now.minute < 30 else 60
    next_run = now.replace(second=0, microsecond=0)
    if minute == 60:
        return next_run.replace(minute=0) + timedelta(hours=1)
    return next_run.replace(minute=30)


async def run_command(command: list[str], timeout: float | None = None) -> CommandResult:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=PROJECT_DIR,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise HTTPException(status_code=504, detail="Command timed out")
    return CommandResult(
        command=command,
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def run_pull_keys_once() -> CommandResult:
    async with pull_lock:
        result = await run_command(
            [PYTHON, str(SCRIPTS_DIR / "pull_keys.py"), "--once"],
            timeout=PULL_KEYS_TIMEOUT,
        )
        # If pull succeeded and PUBLIC_BASE_URL is configured, pre-encrypt links
        if result.returncode == 0 and PUBLIC_BASE_URL:
            try:
                asyncio.create_task(encrypt_links_for_all_users(PUBLIC_BASE_URL))
            except Exception as exc:
                print(f"Failed to schedule encrypt_links task: {exc}", file=sys.stderr)
        return result


async def pull_keys_scheduler() -> None:
    while True:
        now = datetime.now()
        run_at = next_half_hour(now)
        await asyncio.sleep((run_at - now).total_seconds())
        try:
            await run_pull_keys_once()
        except Exception as exc:
            print(f"pull_keys scheduled run failed: {exc}", file=sys.stderr)


async def subscription_expiry_watcher() -> None:
    while True:
        await asyncio.sleep(SUBSCRIPTION_EXPIRY_CHECK_INTERVAL)
        async with build_lock:
            if (
                build_subscription_process.is_running()
                and build_subscription_process.expires_at
                and datetime.now(timezone.utc) >= build_subscription_process.expires_at
            ):
                build_subscription_process.terminate()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler_task, expiry_task
    storage.init_db()
    scheduler_task = asyncio.create_task(pull_keys_scheduler())
    expiry_task = asyncio.create_task(subscription_expiry_watcher())
    try:
        yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
        if expiry_task:
            expiry_task.cancel()
        build_subscription_process.terminate()


app = FastAPI(title="Happ tools backend", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------------------
# Публичные эндпоинты
# ---------------------------------------------------------------------------

@app.get("/", response_class=FileResponse)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------

@app.post("/auth/login", response_model=LoginResponse)
def auth_login(request: Request, body: LoginRequest) -> LoginResponse:
    """Вход через Telegram Mini Apps.

    Принимает initData из window.Telegram.WebApp.initData.
    Сервер проверяет HMAC-подпись и выдаёт токен сессии.
    """
    # Rate limiting по IP
    client_ip = request.client.host if request.client else "unknown"
    check_login_rate_limit(client_ip)

    user = verify_init_data(body.init_data)
    user = storage.upsert_user(user)
    token, expires_at = create_session(user["id"])
    return LoginResponse(
        token=token,
        user=AuthUser(**user),
        expires_at=expires_at,
    )


@app.get("/auth/me", response_model=AuthUser)
def auth_me(user: dict = Depends(get_current_user)) -> AuthUser:
    """Возвращает данные текущего пользователя (из сессии)."""
    return AuthUser(**user)


@app.post("/auth/logout")
def auth_logout(request: Request) -> dict[str, bool]:
    """Завершает сессию."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()
        with sessions_lock:
            sessions.pop(token, None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Защищённые эндпоинты
# ---------------------------------------------------------------------------

@app.get("/keys/status", response_model=KeysStatus)
def keys_status(user: dict = Depends(get_current_user)) -> KeysStatus:
    path = storage.KEYS_JSON_PATH
    latest = storage.latest_key_update()
    total_keys = latest["total_keys"] if latest and latest["status"] == "ok" else 0
    if not path.exists() or not total_keys:
        return KeysStatus(path=str(path), exists=False, total_keys=total_keys)
    stat = path.stat()
    updated_at = None
    if latest and latest.get("pulled_at"):
        updated_at = datetime.fromisoformat(latest["pulled_at"])
    return KeysStatus(
        path=str(path),
        exists=True,
        updated_at=updated_at or datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        size_bytes=stat.st_size,
        total_keys=total_keys,
    )


@app.post("/pull-keys", response_model=CommandResult)
async def pull_keys(user: dict = Depends(get_current_user)) -> CommandResult:
    return await run_pull_keys_once()


async def start_build_subscription(request: BuildSubscriptionRequest) -> ManagedScriptStatus:
    async with build_lock:
        if build_subscription_process.is_running():
            if not request.force_restart:
                return build_subscription_process.status()
            build_subscription_process.terminate()

        command = [
            PYTHON,
            "-u",
            str(SCRIPTS_DIR / "build_subscription.py"),
            "--serve",
            "--port",
            str(request.port),
        ]
        if request.name:
            command.extend(["--name", request.name])
        if request.slug:
            command.extend(["--slug", request.slug])
        if request.profile_title:
            command.extend(["--profile-title", request.profile_title])

        build_subscription_process.start(command, ttl_seconds=request.active_minutes * 60, cwd=DATA_DIR)
        return build_subscription_process.status()


@app.post("/build-subscription", response_model=ManagedScriptStatus)
async def build_subscription(
    request: BuildSubscriptionRequest,
    user: dict = Depends(get_current_user),
) -> ManagedScriptStatus:
    return await start_build_subscription(request)


@app.get("/build-subscription/status", response_model=ManagedScriptStatus)
def build_subscription_status(user: dict = Depends(get_current_user)) -> ManagedScriptStatus:
    return build_subscription_process.status()


@app.post("/build-subscription/stop", response_model=ManagedScriptStatus)
async def stop_build_subscription(user: dict = Depends(get_current_user)) -> ManagedScriptStatus:
    async with build_lock:
        build_subscription_process.terminate()
        return build_subscription_process.status()


# ---------------------------------------------------------------------------
# Файлы подписки (защищены: сессия ИЛИ подписанный URL для Happ)
# ---------------------------------------------------------------------------

@app.get("/subscription.txt", response_class=PlainTextResponse)
def subscription_txt(request: Request) -> PlainTextResponse:
    """Отдаёт подписку с заголовком #profile-title: OpenGate <user_id>."""
    user_id = verify_signed_file(request, "subscription.txt")
    if user_id is None:
        user_id = get_current_user(request)["id"]
    if not storage.user_has_access(user_id):
        raise HTTPException(status_code=403, detail="Subscription is not active")
    body = storage.build_subscription_text(user_id)
    if not body:
        raise HTTPException(status_code=404, detail="Keys not found. Run POST /pull-keys first.")
    return PlainTextResponse(body, headers=storage.subscription_headers(user_id))


@app.get("/subscription.json", response_class=JSONResponse)
def subscription_json(request: Request) -> JSONResponse:
    user_id = verify_signed_file(request, "subscription.json")
    if user_id is None:
        user_id = get_current_user(request)["id"]
    if not storage.user_has_access(user_id):
        raise HTTPException(status_code=403, detail="Subscription is not active")
    payload = storage.build_subscription_json(user_id)
    if not payload["servers"]:
        raise HTTPException(status_code=404, detail="Keys not found. Run POST /pull-keys first.")
    return JSONResponse(payload, headers=storage.subscription_headers(user_id))


# ---------------------------------------------------------------------------
# Ссылки подписки (защищены)
# ---------------------------------------------------------------------------

def call_crypto_api(target_url: str, api_url: str = "https://crypto.happ.su/api-v2.php", timeout: int = 15) -> str:
    """Call the Happ crypto API and return the encrypted subscription link."""
    payload = json.dumps({"url": target_url}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(api_url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


async def encrypt_links_for_all_users(base_url: str) -> None:
    """Precompute encrypted happ:// links for all active users and store them in DB.

    Uses a semaphore to limit concurrency and avoid overwhelming the crypto API.
    """
    user_ids = storage.list_active_user_ids()
    if not user_ids:
        return

    # Limit concurrent crypto API calls to avoid rate limiting by the API
    sem = asyncio.Semaphore(CRYPTO_API_CONCURRENCY)
    now = int(time.time())

    async def encrypt_one(uid: int) -> None:
        """Encrypt a single user's subscription link."""
        async with sem:
            try:
                expires = now + SUBSCRIPTION_LINK_TTL
                payload = f"subscription.txt:{uid}:{expires}"
                sig = hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()
                signed_url = f"{base_url.rstrip('/')}/subscription.txt?expires={expires}&uid={uid}&sig={sig}"

                # call crypto API in thread to avoid blocking event loop
                try:
                    resp = await asyncio.to_thread(call_crypto_api, signed_url)
                except Exception as e:
                    print(f"crypto API call failed for uid={uid}: {e}", file=sys.stderr)
                    return

                encrypted = resp.strip()
                if encrypted.startswith('{'):
                    try:
                        data = json.loads(encrypted)
                        encrypted = (data.get('encrypted_link') or '').strip()
                    except Exception:
                        encrypted = ''
                if encrypted.startswith('happ://'):
                    # store link and cache in memory
                    await asyncio.to_thread(storage.upsert_encrypted_link, uid, encrypted, str(expires))
                    set_cached_encrypted_link(uid, encrypted, expires)
            except Exception as exc:
                print(f"encrypt_links_for_all_users error for uid={uid}: {exc}", file=sys.stderr)

    # Run all encryption tasks concurrently with bounded concurrency
    await asyncio.gather(*(encrypt_one(uid) for uid in user_ids))


def _get_deep_link(request: Request, user_id: int) -> str:
    """Возвращает рабочую Happ deep link.

    Всегда шифрует подписанный URL на наш бэкенд, чтобы Happ скачивал
    файл по защищённой ссылке, а не через незащищённый ngrok-туннель.
    user_id зашит в подпись, чтобы подписка пришла с OpenGate <user_id>.
    """
    # 1) Check in-memory cache first (fastest)
    cached = get_cached_encrypted_link(user_id)
    if cached:
        return cached

    # 2) Check DB for precomputed encrypted link
    try:
        row = storage.get_encrypted_link(user_id)
        if row and row.get("encrypted_link"):
            try:
                expires = int(row.get("expires_at") or "0")
            except Exception:
                expires = 0
            if expires and expires > int(time.time()):
                # Cache in memory with a shorter TTL than the link expiry
                cache_expires = min(expires, int(time.time()) + ENCRYPTED_LINK_CACHE_TTL)
                set_cached_encrypted_link(user_id, row["encrypted_link"], cache_expires)
                return row["encrypted_link"]
    except Exception:
        pass

    # 3) Fallback: sign the plain-text subscription URL and try to encrypt on the fly
    target_url = make_signed_subscription_url(request, "subscription.txt", user_id)
    try:
        response = call_crypto_api(target_url)
        encrypted = response.strip()
        if encrypted.startswith("{"):
            data = json.loads(encrypted)
            encrypted = (data.get("encrypted_link") or "").strip()
        if encrypted.startswith("happ://"):
            # Cache the freshly encrypted link
            cache_expires = int(time.time()) + ENCRYPTED_LINK_CACHE_TTL
            set_cached_encrypted_link(user_id, encrypted, cache_expires)
            return encrypted
    except Exception:
        pass
    encoded = urllib.parse.quote(target_url, safe=":")
    return f"happ://add/{encoded}"


@app.get("/subscription-link", response_class=PlainTextResponse)
def subscription_link(request: Request, user: dict = Depends(get_current_user)) -> str:
    """Возвращает рабочую Happ deep link для текущего пользователя."""
    return _get_deep_link(request, user["id"])


@app.get("/subscription-redirect-302")
def subscription_redirect_302(request: Request) -> RedirectResponse:
    """Мгновенный 302-редирект на Happ deep link.

    Открывается через tg.openLink() в системном браузере. Сервер сразу
    возвращает 302 на happ:// — браузер следует за редиректом и показывает
    системный промпт "Открыть в Happ?" без промежуточной HTML-страницы.
    """
    user_id = verify_signed_redirect(request)
    if user_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    target = _get_deep_link(request, user_id)
    return RedirectResponse(target, status_code=302)


@app.get("/subscription-redirect-url", response_class=PlainTextResponse)
def subscription_redirect_url(
    request: Request,
    user: dict = Depends(get_current_user),
) -> str:
    """Возвращает подписанный URL на /subscription-redirect.

    Фронт открывает этот URL через tg.openLink() в системном браузере,
    где нет Bearer-токена, поэтому авторизация — это подпись в URL.
    """
    return make_signed_redirect_url(request, user["id"])




@app.post("/happ-ping", response_model=CommandResult)
async def happ_ping(
    region: str = "all",
    limit: int = Query(default=20, ge=1, le=500),
    timeout: float = Query(default=3.0, ge=0.1, le=30.0),
    mode: Literal["tcp", "icmp", "via"] = "tcp",
    via_method: Literal["get", "head", "tls"] = "get",
    user: dict = Depends(get_current_user),
) -> CommandResult:
    command = [
        PYTHON,
        str(SCRIPTS_DIR / "happ_ping.py"),
        "--region",
        region,
        "--limit",
        str(limit),
        "--timeout",
        str(timeout),
        "--mode",
        mode,
        "--via-method",
        via_method,
    ]
    return await run_command(command, timeout=max(60, limit * (timeout + 1)))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False)
