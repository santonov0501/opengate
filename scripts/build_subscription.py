#!/usr/bin/env python3
"""build_subscription.py

Utility script to encrypt subscription URLs using the Happ crypto API.
The actual subscription serving is handled by the FastAPI backend (app/main.py).
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import storage


def call_crypto_api(api_url: str, target_url: str, timeout: int = 15) -> str:
    """Call the Happ crypto API and return the encrypted subscription link."""
    import urllib.request
    import urllib.error

    payload = json.dumps({"url": target_url}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/json",
    }
    req = urllib.request.Request(api_url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP error: {e.code} {e.reason}")
    except Exception as e:
        raise RuntimeError(f"Error calling crypto API: {e}")


def make_deep_link(target_url: str) -> str:
    """Build a universal Happ deep link to add a subscription."""
    import urllib.parse
    encoded = urllib.parse.quote(target_url, safe=":")
    return f"happ://add/{encoded}"


def make_encrypted_deep_link(api_response: str, target_url: str) -> tuple[str, str]:
    """Extract the encrypted Happ link from the crypto API response."""
    encrypted_link = api_response.strip()
    if encrypted_link.startswith("{"):
        try:
            data = json.loads(encrypted_link)
            encrypted_link = (data.get("encrypted_link") or "").strip()
        except json.JSONDecodeError:
            encrypted_link = ""
    if encrypted_link.startswith("happ://"):
        return encrypted_link, encrypted_link
    return make_deep_link(target_url), encrypted_link


def main() -> int:
    p = argparse.ArgumentParser(description="Encrypt Happ subscription URL using crypto API")
    p.add_argument("--url", required=True, help="Public HTTPS URL to subscription file")
    p.add_argument("--api", default="https://crypto.happ.su/api-v2.php", help="Crypto API endpoint")
    args = p.parse_args()

    target = args.url
    if not target.startswith("https://"):
        print("Happ requires the subscription URL to be https:// (not http://).", file=sys.stderr)
        return 1

    print(f"Calling crypto API {args.api} for {target} ...")
    try:
        resp = call_crypto_api(args.api, target)
        print("Crypto API response:")
        print(resp)
        encrypted_link, _ = make_encrypted_deep_link(resp, target)
        print(f"\nEncrypted subscription link (iOS & Android): {encrypted_link}")

        plain_deep = make_deep_link(target)
        print(f"Plain deep link (fallback): {plain_deep}")
        return 0
    except Exception as e:
        print(f"Encryption failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())