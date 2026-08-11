import argparse
import json
import csv
import re
import socket
import subprocess
import ssl
import time
import threading
import os
from datetime import datetime
from pathlib import Path
import sys
import ctypes
from urllib.parse import urlparse

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app import storage


def load_keys(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_vless_host_port(key: str):
    """Extract host and port from a vless URI string. Returns (host, port) or (None, None)."""
    if not key or not isinstance(key, str):
        return None, None
    # Try urlparse
    try:
        p = urlparse(key)
        netloc = p.netloc  # may include userinfo
        if '@' in netloc:
            hostport = netloc.split('@', 1)[1]
        else:
            hostport = netloc
        if ':' in hostport:
            host, port = hostport.rsplit(':', 1)
            return host, int(port)
    except Exception:
        pass

    # Fallback regex
    m = re.search(r"@([^:/?#]+):(\d+)", key)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


def load_keys_csv(path: Path):
    """Read `keys_simple.csv` with header `no,name,key` and return list of entries dicts.

    Each entry: {'region': name, 'host': host, 'port': port, 'key': key}
    """
    entries = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if not row:
                continue
            # expect at least 3 columns: no,name,key
            if len(row) < 3:
                continue
            name = row[1]
            key = row[2]
            host, port = parse_vless_host_port(key)
            if host and port:
                entries.append({
                    'region': name,
                    'host': host,
                    'port': port,
                    'key': key,
                })
    return entries


def extract_endpoints(data):
    endpoints = []

    for region, region_data in data.items():
        if not isinstance(region_data, dict):
            continue

        best = region_data.get("best")
        if best:
            endpoints.append((region, best, region_data.get("top10", [])))
        else:
            endpoints.append((region, None, region_data.get("top10", [])))

    return endpoints


def normalize_entries(region, top10):
    entries = []

    for item in top10:
        host = item.get("host")
        port = item.get("port")
        if host and port:
            entries.append({
                "region": region,
                "host": host,
                "port": port,
                "key": item.get("key"),
            })

    return entries


def ping_tcp(host: str, port: int, timeout: float = 3.0):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
        elapsed = (time.perf_counter() - start) * 1000
        return True, round(elapsed, 1)
    except socket.timeout:
        return False, None
    except OSError:
        return False, None


def ping_icmp(host: str, timeout: float = 3.0):
    """Use system ping to send a single ICMP echo and parse time in ms.

    Returns (ok: bool, latency_ms: float|None)
    """
    # cross-platform ping: Windows uses '-n 1 -w timeout_ms', Unix uses '-c 1 -W timeout_s'
    if os.name == 'nt':
        cmd = ['ping', '-n', '1', '-w', str(int(timeout * 1000)), host]
    else:
        # '-W' in many unices expects seconds (integer) or deciseconds on some systems; use timeout as int seconds
        cmd = ['ping', '-c', '1', '-W', str(int(max(1, timeout))), host]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 1)
        out = proc.stdout
        if proc.returncode != 0:
            return False, None

        # try to extract time=XXms or time=XX ms
        m = re.search(r'time[=|<]\s*(\d+(?:[\.,]\d+)?)\s*ms', out)
        if not m:
            # try alternative like = XX ms
            m = re.search(r'time[:=]\s*(\d+(?:[\.,]\d+)?)', out)
        if m:
            val = float(m.group(1).replace(',', '.'))
            return True, round(val, 1)
        return True, None
    except (subprocess.SubprocessError, OSError):
        return False, None


def ping_tls(host: str, port: int, timeout: float = 3.0):
    """Measure TLS handshake time (approximate) to host:port.

    Returns (ok, latency_ms)
    """
    ctx = ssl.create_default_context()
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, server_hostname=host if not re.match(r"^\d+\.\d+\.\d+\.\d+$", host) else None):
                # handshake completes on enter
                pass
        elapsed = (time.perf_counter() - start) * 1000
        return True, round(elapsed, 1)
    except (ssl.SSLError, socket.timeout, OSError):
        return False, None


def ping_via_proxy_like(host: str, port: int, timeout: float = 5.0, method: str = 'get'):
    """Attempt a Via-Proxy style check: establish TCP/TLS and send minimal HTTP GET/HEAD to '/'.

    This approximates the HApp "Via Proxy" GET/HEAD by performing a request and measuring time
    until first response bytes are received. Note: this is a best-effort approximation when no
    actual proxy chaining is available.
    """
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            # try TLS first (many server proxies use TLS)
            try:
                ctx = ssl.create_default_context()
                sock = ctx.wrap_socket(sock, server_hostname=host if not re.match(r"^\d+\.\d+\.\d+\.\d+$", host) else None)
            except Exception:
                # fall back to plain socket
                pass

            # send simple HTTP request to host root
            req_method = 'HEAD' if method.lower() == 'head' else 'GET'
            req = f"{req_method} / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
            sock.sendall(req.encode('utf-8'))
            # wait for any response bytes
            chunk = sock.recv(1)
            if not chunk:
                return False, None
        elapsed = (time.perf_counter() - start) * 1000
        return True, round(elapsed, 1)
    except (socket.timeout, OSError, ssl.SSLError):
        return False, None


def perform_ping(host: str, port: int, mode: str = 'tcp', timeout: float = 3.0, via_method: str = 'get'):
    """Dispatch to requested ping implementation.

    mode: 'tcp'|'icmp'|'via'
    via_method: 'get'|'head'|'tls'
    """
    mode = (mode or 'tcp').lower()
    if mode == 'icmp':
        return ping_icmp(host, timeout=timeout)
    if mode == 'tcp':
        return ping_tcp(host, port, timeout=timeout)
    if mode == 'via':
        if via_method == 'tls':
            return ping_tls(host, port, timeout=timeout)
        return ping_via_proxy_like(host, port, timeout=timeout, method=via_method)
    # default
    return ping_tcp(host, port, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description="HApp-like TCP ping client для серверов из keys.json")
    parser.add_argument("--region", default="all", help="Регион для проверки (по умолчанию all)")
    parser.add_argument("--limit", type=int, default=20, help="Максимальное количество хостов для проверки")
    parser.add_argument("--timeout", type=float, default=3.0, help="Таймаут TCP соединения в секундах")
    parser.add_argument("--mode", choices=["tcp", "icmp", "via"], default="tcp", help="Режим пинга: tcp, icmp или via (Via Proxy-like)")
    parser.add_argument("--via-method", choices=["get", "head", "tls"], default="get", help="Метод для режима via: GET, HEAD или TLS-handshake")
    parser.add_argument("--live", action="store_true", help="Запускать в режиме живого обновления (интервал обновления дисплея) ")
    parser.add_argument("--refresh", type=float, default=1.0, help="Интервал обновления дисплея в секундах (по умолчанию 1)")
    parser.add_argument("--ping-interval", type=float, default=5.0, help="Интервал между пингами одного хоста в секундах (по умолчанию 5)")
    args = parser.parse_args()

    entries = []
    for row in storage.list_key_rows():
        if args.region != "all" and args.region not in {row["region"], row["region_name"]}:
            continue
        host = row["host"]
        port = row["port"]
        if not host or not port:
            host, port = parse_vless_host_port(row["uri"])
        if host and port:
            entries.append(
                {
                    "region": row["region_name"] or row["region"],
                    "host": host,
                    "port": port,
                    "key": row["uri"],
                }
            )

    if not entries:
        print("Серверы не найдены. Проверьте файл или указанный регион.")
        return

    # limit entries
    entries = entries[: args.limit]

    if not args.live:
        print(f"Checking {len(entries)} servers from {storage.DB_PATH}...")
        print(f"{'REGION':<15} {'HOST':<30} {'PORT':<6} {'STATUS':<8} {'LATENCY_MS':<10}")
        print("-" * 100)

        for entry in entries:
            host = entry["host"]
            port = entry["port"]
            ok, latency = perform_ping(host, port, mode=args.mode, timeout=args.timeout, via_method=args.via_method)
            status = "OK" if ok else "DOWN"
            latency_text = f"{latency:.1f}" if latency is not None else "-"
            print(f"{entry['region']:<15} {host:<30} {port:<6} {status:<8} {latency_text:<10}")

        print("Done.")
        return

    # Live mode: spawn a thread per entry that periodically pings and stores last result
    stop_event = threading.Event()
    results = {}

    def worker(e):
        host = e['host']
        port = e['port']
        key = e.get('key') or ''
        region = e.get('region')
        while not stop_event.is_set():
            ok, latency = perform_ping(host, port, mode=args.mode, timeout=args.timeout, via_method=args.via_method)
            results[(host, port)] = {
                'region': region,
                'host': host,
                'port': port,
                'status': 'OK' if ok else 'DOWN',
                'latency': latency,
                'key': key,
                'ts': datetime.utcnow(),
            }
            # sleep between pings for this host
            for _ in range(int(max(1, args.ping_interval))):
                if stop_event.is_set():
                    break
                time.sleep(1)

    threads = []
    for entry in entries:
        t = threading.Thread(target=worker, args=(entry,), daemon=True)
        t.start()
        threads.append(t)

    try:
        # Enable VT processing on Windows for ANSI escapes
        if os.name == 'nt':
            try:
                kernel32 = ctypes.windll.kernel32
                hStdOut = kernel32.GetStdHandle(-11)
                mode = ctypes.c_uint()
                if kernel32.GetConsoleMode(hStdOut, ctypes.byref(mode)):
                    ENABLE_VT_PROCESSING = 0x0004
                    new_mode = mode.value | ENABLE_VT_PROCESSING
                    kernel32.SetConsoleMode(hStdOut, new_mode)
            except Exception:
                pass

        printed_lines = 0
        header_lines = 3  # title + header + separator

        while not stop_event.is_set():
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')

            # Move cursor up to overwrite previous block instead of appending
            if printed_lines > 0:
                # move cursor up by printed_lines
                sys.stdout.write(f"\033[{printed_lines}A")

            lines = []
            lines.append(f"Live HApp ping — {now} — обновление каждые {args.refresh}s")
            lines.append(f"{'REGION':<15} {'HOST':<30} {'PORT':<6} {'STATUS':<8} {'LATENCY_MS':<10} {'AGE_S':<6}")
            lines.append('-' * 160)

            for entry in entries:
                host = entry['host']
                port = entry['port']
                r = results.get((host, port))
                if r is None:
                    status = 'N/A'
                    latency_text = '-'
                    age = '-'
                    key = entry.get('key') or ''
                    region = entry.get('region')
                else:
                    status = r['status']
                    latency_text = f"{r['latency']:.1f}" if r['latency'] is not None else '-'
                    age = int((datetime.utcnow() - r['ts']).total_seconds())
                    key = r.get('key') or ''
                    region = r.get('region')

                lines.append(f"{region:<15} {host:<30} {port:<6} {status:<8} {latency_text:<10} {age:<6}")

            # Print lines, clearing each line to avoid remnants
            for line in lines:
                sys.stdout.write(line)
                sys.stdout.write('\033[K')
                sys.stdout.write('\n')

            sys.stdout.flush()
            printed_lines = len(lines)

            time.sleep(args.refresh)
    except KeyboardInterrupt:
        stop_event.set()
        print('\nStopping...')
        for t in threads:
            t.join(timeout=1)


if __name__ == "__main__":
    main()
