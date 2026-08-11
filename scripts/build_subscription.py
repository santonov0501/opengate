#!/usr/bin/env python3
"""build_subscription.py

Creates subscription files from keys stored in data/keys.json (or DB fallback):
 - subscription.txt
 - subscription.json

Optional: serve files via local HTTP server and call Happ crypto API to encrypt link.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time
import http.server
import urllib.request
import urllib.error
import unicodedata
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import storage


def slugify(value: str, default: str = 'subscription') -> str:
    normalized = unicodedata.normalize('NFKD', value)
    ascii_only = normalized.encode('ascii', 'ignore').decode('ascii')
    slug = re.sub(r'[^a-z0-9]+', '-', ascii_only.lower()).strip('-')
    return slug or default


def get_local_ip() -> str:
    # best-effort, does not make network call to remote
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def sanitize_http_header_value(value: str) -> str:
    """Sanitize HTTP header value to prevent header injection (CRLF)."""
    return value.replace('\r', '').replace('\n', '')


def make_subscription_handler(slug: str, profile_title: str):
    """Create an HTTP handler that serves subscription data directly from the DB.

    Instead of generating files on disk, this handler builds the subscription
    content on-the-fly from the SQLite database on every request.
    """
    class SubscriptionHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                path = self.path.split('?', 1)[0].rstrip('/')
                rows = storage.list_key_rows()
                print(f"[subscription] {self.path} -> keys={len(rows)}", file=sys.stderr)
                if not rows:
                    self.send_error(404, "No keys found")
                    return
                keys = [row["uri"] for row in rows]
                if path.endswith('.json'):
                    servers = []
                    for row in rows:
                        servers.append({
                            "uri": row["uri"],
                            "remarks": row["region_name"] or row["region"],
                            "meta": {
                                "serverDescription": row["region_name"] or row["region"] or "VLESS",
                            },
                        })
                    params = {"profile-title": profile_title[:25]}
                    params.update(storage.default_subscription_settings())
                    payload = {
                        "subscription": {
                            "id": "default",
                            "status": "active",
                            "params": params,
                        },
                        "servers": servers,
                    }
                    body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('content-disposition', 'attachment; filename="subscription.json"')
                    for key, value in params.items():
                        sanitized_value = sanitize_http_header_value(str(value))
                        self.send_header(str(key), sanitized_value)
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    params = {"profile-title": profile_title[:25]}
                    params.update(storage.default_subscription_settings())
                    header_lines = [f"#{key}: {value}" for key, value in sorted(params.items())]
                    body = ("\n".join(header_lines) + "\n" + "\n".join(keys) + "\n").encode('utf-8')
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('content-disposition', 'attachment; filename="subscription.txt"')
                    self.end_headers()
                    self.wfile.write(body)
            except Exception as exc:
                print(f"[subscription] error: {exc}", file=sys.stderr)
                self.send_error(500, str(exc))

        def log_message(self, format, *args):
            pass

    return SubscriptionHandler


def start_server(handler, port: int) -> threading.Thread:
    server = http.server.ThreadingHTTPServer(('0.0.0.0', port), handler)

    def run():
        try:
            server.serve_forever()
        finally:
            server.server_close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t




def call_crypto_api(api_url: str, target_url: str, timeout: int = 15) -> str:
    payload = json.dumps({'url': target_url}).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        # The API is behind Cloudflare and rejects requests without a browser-like User-Agent (error 1010)
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
        'Accept': 'application/json',
    }
    req = urllib.request.Request(api_url, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP error: {e.code} {e.reason}')
    except Exception as e:
        raise RuntimeError(f'Error calling crypto API: {e}')


def make_deep_link(target_url: str) -> str:
    """Build a universal Happ deep link to add a subscription.

    Works on both iOS and Android (same scheme).
    Format: happ://add/<URL-encoded subscription URL>
    """
    encoded = urllib.parse.quote(target_url, safe=":")
    return f'happ://add/{encoded}'


def make_encrypted_deep_link(api_response: str, target_url: str) -> tuple[str, str]:
    """Extract the encrypted Happ link from the crypto API response.

    The API returns JSON like {"encrypted_link": "happ://crypt5/..."}.
    The encrypted link (happ://crypt5/...) is used directly as the
    subscription link — it is NOT wrapped into happ://add/ and NOT
    URL-encoded, because Happ accepts it as-is.

    Returns (encrypted_link, encrypted_link).
    """
    encrypted_link = api_response.strip()
    # API may return JSON: {"encrypted_link": "happ://..."}
    if encrypted_link.startswith('{'):
        try:
            data = json.loads(encrypted_link)
            encrypted_link = (data.get('encrypted_link') or '').strip()
        except json.JSONDecodeError:
            encrypted_link = ''
    if encrypted_link.startswith('happ://'):
        # The encrypted link is the subscription link itself
        return encrypted_link, encrypted_link
    # API returned something else (e.g. plain URL) — fall back to plain deep link
    return make_deep_link(target_url), encrypted_link


def main() -> int:
    p = argparse.ArgumentParser(description='Build Happ subscription from keys read from data/keys.json (or DB)')
    p.add_argument('--name', help='Friendly subscription name used for profile title and default filename')
    p.add_argument('--slug', help='Optional URL-safe filename base for the served subscription file')
    p.add_argument('--profile-title', help='Explicit subscription profile title inserted into the body')
    p.add_argument('--serve', action='store_true', help='Start local HTTP server to serve files')
    p.add_argument('--port', type=int, default=8000, help='Port for local HTTP server')
    p.add_argument('--encrypt', action='store_true', help='Call crypto API to encrypt the hosted URL')
    p.add_argument('--api', default='https://crypto.happ.su/api-v2.php', help='Crypto API endpoint')
    p.add_argument('--url', help='Public URL to subscription file (required for --encrypt if not serving locally)')
    args = p.parse_args()

    rows = storage.list_key_rows()
    if not rows:
        print('No keys found. Run pull_keys first.', file=sys.stderr)
        return 3

    slug = None
    if args.slug:
        slug = slugify(args.slug)
    elif args.name:
        slug = slugify(args.name)
    elif args.profile_title:
        slug = slugify(args.profile_title)

    profile_title = args.profile_title or args.name or 'Happ Subscription'

    server_thread = None
    served_url = None
    if args.serve:
        handler = make_subscription_handler(slug, profile_title)
        server_thread = start_server(handler, args.port)
        json_name = f'{slug}.json' if slug else 'subscription.json'
        local_ip = get_local_ip()
        served_url = f'http://{local_ip}:{args.port}/{json_name}'
        print(f'Serving subscription on port {args.port} (HTTP)')
        print(f'Local URL: {served_url}')

        deep_link = make_deep_link(served_url)
        print(f'iOS deep link:  {deep_link}')
        print(f'Android deep link: {deep_link}')

    # Auto-encrypt when --encrypt is passed
    if args.encrypt:
        if not args.url and not served_url:
            print('Encryption requested but no public URL provided. Use --url or --serve and expose it.', file=sys.stderr)
            return 4
        target = args.url or served_url
        if not target.startswith('https://'):
            print('Happ requires the subscription URL to be https:// (not http://).', file=sys.stderr)
            print('Expose your subscription over HTTPS (e.g. Railway, GitHub Pages, etc.) and pass --url https://...', file=sys.stderr)
            return 6
        if args.url:
            plain_deep = make_deep_link(args.url)
            print(f'Suggested Happ deep link: {plain_deep}')
        print(f'Calling crypto API {args.api} for {target} ...')
        try:
            resp = call_crypto_api(args.api, target)
            print('Crypto API response:')
            print(resp)
            encrypted_link, _ = make_encrypted_deep_link(resp, target)
            print(f'Encrypted subscription link (iOS & Android): {encrypted_link}')
        except Exception as e:
            print(f'Encryption failed: {e}', file=sys.stderr)
            return 5

    # keep server running if started
    try:
        if server_thread:
            print('Press Ctrl-C to stop server and exit')
            server_thread.join()
    except KeyboardInterrupt:
        print('\nStopping...')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
