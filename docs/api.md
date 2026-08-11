# API Endpoints

Base URL for local development:

```text
http://127.0.0.1:8080
```

In Postman, set `Content-Type: application/json` for requests with a JSON body.

## GET /health

Checks that the backend is running.

Postman:

```text
Method: GET
URL: http://127.0.0.1:8080/health
```

Example response:

```json
{
  "status": "ok"
}
```

## Data Storage

Persistent data is stored in `data/app.db` (SQLite):

- `users`: Telegram users and their access status.
- `subscriptions`: one shared `default` subscription, currently `active`.
- `keys`: the current key set. `POST /pull-keys` fully replaces this table.
- `key_updates`: update history for key pulls.

`TELEGRAM_ALLOWED_IDS` remains a safety allowlist. If it is set, only listed Telegram IDs can log in; after login, access is also checked through `users.access_status` and the shared subscription status.

## POST /pull-keys

Runs `scripts/pull_keys.py --once`.

The script downloads keys from a remote source and fully replaces the `keys` table in `data/app.db`.

Postman:

```text
Method: POST
URL: http://127.0.0.1:8080/pull-keys
```

Body: none.

Example response:

```json
{
  "command": ["python", "C:\\claude\\scripts\\pull_keys.py", "--once"],
  "returncode": 0,
  "stdout": "...",
  "stderr": ""
}
```

## POST /build-subscription

Starts `scripts/build_subscription.py` with `--serve --https`.

The script builds `data/subscription.txt` and `data/subscription.json`, starts a local HTTP server from the `data/` directory, opens an ngrok HTTPS tunnel, and calls the Happ crypto API. The backend keeps the process active for `active_minutes`, then stops it.

Postman:

```text
Method: POST
URL: http://127.0.0.1:8080/build-subscription
Header: Content-Type: application/json
```

Body:

```json
{
  "active_minutes": 5,
  "port": 8000,
  "ngrok_api_port": 4040,
  "force_restart": false
}
```

Optional body fields:

```json
{
  "active_minutes": 5,
  "port": 8000,
  "ngrok_api_port": 4040,
  "name": "My Happ Subscription",
  "slug": "my-happ-subscription",
  "profile_title": "My Happ Subscription",
  "force_restart": true
}
```

Example response:

```json
{
  "running": true,
  "pid": 12345,
  "started_at": "2026-08-07T09:00:00.000000Z",
  "expires_at": "2026-08-07T09:05:00.000000Z",
  "public_url": null,
  "returncode": null,
  "command": ["python", "-u", "C:\\claude\\scripts\\build_subscription.py", "--serve", "--https"],
  "logs": []
}
```

Notes:

- `public_url` can be `null` immediately after start. Use `GET /build-subscription/status` to poll until ngrok URL appears.
- If a process is already running and `force_restart` is `false`, the endpoint returns the current status instead of starting a second process.
- If `force_restart` is `true`, the backend stops the existing process and starts a new one.

## GET /build-subscription/status

Returns the current `build_subscription.py` process status, ngrok URL, command, and recent logs.

Postman:

```text
Method: GET
URL: http://127.0.0.1:8080/build-subscription/status
```

Example response:

```json
{
  "running": true,
  "pid": 12345,
  "started_at": "2026-08-07T09:00:00.000000Z",
  "expires_at": "2026-08-07T09:05:00.000000Z",
  "public_url": "https://example.ngrok-free.dev/subscription.txt",
  "returncode": null,
  "command": ["python", "-u", "C:\\claude\\scripts\\build_subscription.py", "--serve", "--https"],
  "logs": [
    "Wrote C:\\claude\\data\\subscription.txt (34 entries)",
    "Public HTTPS URL (ngrok): https://example.ngrok-free.dev/subscription.txt"
  ]
}
```

## POST /build-subscription/stop

Stops the running `build_subscription.py` process and its child processes, including ngrok.

Postman:

```text
Method: POST
URL: http://127.0.0.1:8080/build-subscription/stop
```

Body: none.

Example response:

```json
{
  "running": false,
  "pid": 12345,
  "started_at": "2026-08-07T09:00:00.000000Z",
  "expires_at": "2026-08-07T09:05:00.000000Z",
  "public_url": "https://example.ngrok-free.dev/subscription.txt",
  "returncode": 1,
  "command": ["python", "-u", "C:\\claude\\scripts\\build_subscription.py", "--serve", "--https"],
  "logs": ["..."]
}
```

## GET /subscription.txt

Builds a text subscription from keys stored in SQLite. The response includes Happ body parameters like `#profile-title` and `#profile-update-interval`.

Postman:

```text
Method: GET
URL: http://127.0.0.1:8080/subscription.txt
```

Response type: `text/plain` (one VLESS URI per line, with optional `#profile-title:` header).

If keys do not exist yet, returns `404` with a message to run `POST /pull-keys` first.

## GET /subscription.json

Builds a JSON subscription from keys stored in SQLite. Happ parameters are included in the JSON body and response headers.

Postman:

```text
Method: GET
URL: http://127.0.0.1:8080/subscription.json
```

Response type: `application/json`.

If keys do not exist yet, returns `404` with a message to run `POST /pull-keys` first.

## GET /subscription-link

Returns a working Happ deep link to add the JSON subscription from the backend's current public URL (the URL the request came in through).

It prefers a live encrypted link (`happ://crypt5/...`) from a running build. If none is running, it calls the Happ crypto API to encrypt the backend's own public subscription URL and returns the resulting `happ://crypt5/...` link. It falls back to the plain deep link `happ://add/<public URL>` only if encryption fails.

Postman:

```text
Method: GET
URL: http://127.0.0.1:8080/subscription-link
```

Response type: `text/plain`.

Example response:

```text
happ://crypt5/fzvd6ovH6kXv8JwfbZTkkt9l1469Bt4ur1mGJXcE7rRtyf5RA9FXt0b0X82pvg...
```

## GET /subscription-page

Browser helper page. It starts `POST /build-subscription` with `active_minutes: 5`, displays logs and the ngrok URL, polls status every 2 seconds, and refreshes the page every 5 minutes.

Postman:

```text
Method: GET
URL: http://127.0.0.1:8080/subscription-page
```

Response type: HTML.

## POST /happ-ping

Runs `scripts/happ_ping.py`.

Reads keys from the SQLite database (`app.db`).

Postman:

```text
Method: POST
URL: http://127.0.0.1:8080/happ-ping?limit=20&timeout=3&mode=tcp&region=all&via_method=get
```

Body: none.

Query parameters:

```text
region=all
limit=20
timeout=3
mode=tcp
via_method=get
```

Allowed values:

```text
mode: tcp, icmp, via
via_method: get, head, tls
```

Example response:

```json
{
  "command": ["python", "C:\\claude\\scripts\\happ_ping.py", "--region", "all", "--limit", "20"],
  "returncode": 0,
  "stdout": "Checking 20 servers from app.db...",
  "stderr": ""
}
```
