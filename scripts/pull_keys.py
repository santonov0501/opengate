import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import storage


GITHUB_RAW_URL = (
    "https://raw.githubusercontent.com/tiagorrg/vless-checker/main/docs/keys.json"
)

REGION_NAMES = {
    "baltics": "Baltics",
    "finland": "Finland",
    "sweden": "Sweden",
    "netherlands": "Netherlands",
    "poland": "Poland",
    "germany": "Germany",
    "w_germany": "West Germany",
    "w_netherlands": "West Netherlands",
    "w_baltics": "West Baltics",
    "w_finland": "West Finland",
    "w_sweden": "West Sweden",
    "w_poland": "West Poland",
    "w_other": "West Other",
    "russia": "Russia",
}


def fetch_keys(url: str, timeout: float = 10.0) -> bool:
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        raw_data = json.loads(response.text)
        entries = storage.normalize_key_entries(raw_data, REGION_NAMES)
        storage.replace_keys(
            entries,
            source_url=url,
            raw_updated_at=raw_data.get("updated_at") if isinstance(raw_data, dict) else None,
        )
        return True
    except requests.RequestException as exc:
        print(f"Download error: {exc}")
        storage.record_key_update_error(url, str(exc))
        return False
    except (json.JSONDecodeError, OSError, sqlite3.Error) as exc:
        print(f"Processing error: {exc}")
        storage.record_key_update_error(url, str(exc))
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download remote keys and write them to data/keys.json (and record update in DB)."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Interval in minutes between downloads.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit.",
    )
    args = parser.parse_args()

    interval_seconds = args.interval * 60

    while True:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Downloading {GITHUB_RAW_URL}")
        ok = fetch_keys(GITHUB_RAW_URL)
        if ok:
            print(f"Updated DB at {storage.DB_PATH.resolve()}")
        else:
            print("Download/update failed. Will retry on the next interval.")

        if args.once:
            break

        print(f"Waiting {args.interval} minutes before next download...")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
