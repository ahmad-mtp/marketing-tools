# Deploying the Apollo webhook receiver

Read this when the dedicated server exists. Until then the SSH tunnel in the
[README](../README.md#phone-numbers) is the stopgap, and it is genuinely a
stopgap: free tunnels get a new random URL on every start and expire after a
few hours. Three died in one afternoon during development.

---

## Why this exists at all

Apollo does not return phone numbers in the enrichment response. It returns
demographics immediately, then delivers phone numbers asynchronously — and it
**rejects the enrichment request outright unless you supply a `webhook_url`**.

The tool does not actually wait to be called. It polls
`GET /api/v1/webhook_result/{request_id}` and merges the phones from there.

So the receiver exists to satisfy a **precondition**, not to do work. That
matters, because it sets a very low bar:

> The URL must exist, be reachable from the public internet over HTTPS, and
> return a non-5xx response to Apollo's POST. It does not need to parse the
> payload, store anything, or be highly available.

If it is down when you start an enrichment, the tool's pre-flight check refuses
to run rather than burning credits.

Everything the receiver stores is a **bonus path**: pushed payloads land in
`data/webhooks/` and `POST /api/sweep-webhooks` merges them, which is useful if
a poll ever times out before Apollo finishes.

---

## What to deploy

`app/webhook_receiver.py` — a standalone FastAPI app with two routes:

| Route | Purpose |
|---|---|
| `GET /` | Health check. The pre-flight hits this. |
| `POST /api/apollo/webhook` | Stores the payload, returns 200. |

It shares `app/config.py` and `app/logs.py`, so deploy the `app/` package, not
the single file.

### Do not deploy the dashboard

The receiver is a separate app **on purpose**. `app/main.py` exposes
`/api/scrape`, `/api/connect` and `/api/export.csv`. Putting those on the
public internet would let anyone with the URL drive a signed-in LinkedIn
browser and download the attendee list.

Expose `webhook_receiver:app`. Never `main:app`.

---

## On the dedicated server

Assumes Debian/Ubuntu, a DNS record pointing at the box, and Python 3.12+.

### 1. Install

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin apollohook
sudo -u apollohook git clone <this-repo> /home/apollohook/marketing-tools
cd /home/apollohook/marketing-tools/event-enrichment
sudo -u apollohook python3.12 -m venv .venv
sudo -u apollohook .venv/bin/pip install -r requirements.txt
```

The receiver needs `fastapi`, `uvicorn` and `python-dotenv`. Playwright is in
`requirements.txt` for the harvester and is dead weight here — harmless, or
strip it if you prefer a lean image.

### 2. Run it as a service

`/etc/systemd/system/apollo-webhook.service`:

```ini
[Unit]
Description=Apollo webhook receiver
After=network.target

[Service]
User=apollohook
WorkingDirectory=/home/apollohook/marketing-tools/event-enrichment
Environment=DATA_DIR=/home/apollohook/apollo-data
ExecStart=/home/apollohook/marketing-tools/event-enrichment/.venv/bin/uvicorn \
          app.webhook_receiver:app --host 127.0.0.1 --port 8787
Restart=always
RestartSec=5

# It writes only to DATA_DIR and needs nothing else.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/apollohook/apollo-data

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /home/apollohook/apollo-data
sudo chown apollohook: /home/apollohook/apollo-data
sudo systemctl enable --now apollo-webhook
```

**Bind to `127.0.0.1`, not `0.0.0.0`.** The reverse proxy is what faces the
internet.

### 3. Terminate TLS

Apollo will not post to plain HTTP. Caddy is the least work because it obtains
and renews the certificate itself:

`/etc/caddy/Caddyfile`:

```
apollo-hook.yourdomain.com {
    reverse_proxy 127.0.0.1:8787
}
```

```bash
sudo systemctl reload caddy
```

<details>
<summary>nginx equivalent</summary>

```nginx
server {
    listen 443 ssl http2;
    server_name apollo-hook.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/apollo-hook.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/apollo-hook.yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Get the certificate with `certbot --nginx -d apollo-hook.yourdomain.com`.
</details>

### 4. Point the tool at it

In the operator's `.env`:

```bash
APOLLO_REVEAL_PHONE=true
APOLLO_WEBHOOK_URL=https://apollo-hook.yourdomain.com/api/apollo/webhook
```

**This never changes again.** That is the entire point of doing this.

### 5. Verify

```bash
# health — what the pre-flight checks
curl https://apollo-hook.yourdomain.com/

# a realistic payload
curl -X POST https://apollo-hook.yourdomain.com/api/apollo/webhook \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"probe","people":[{"id":"x","phone_numbers":[{"sanitized_number":"+15125550000"}]}]}'
```

Expected:

```json
{"ok":true,"service":"apollo-webhook-receiver"}
{"received":1,"with_phones":1,"failure":null,"stored":"probe-….json"}
```

Then from the operator's machine:

```bash
.venv/bin/python -c "
import asyncio,sys; sys.path.insert(0,'.')
from app import apollo, config
print(asyncio.run(apollo.check_webhook_reachable(config.APOLLO_WEBHOOK_URL)))"
```

`(True, 'reachable (HTTP 200)')` means enrichment will start.

---

## Serverless alternatives

The receiver is small enough to reimplement in a few lines on a platform with a
free tier, if a whole server is overkill.

| Platform | Notes |
|---|---|
| Cloudflare Workers | No server to maintain. Rewrite the handler in JS; store to R2 or KV, or just return 200 |
| Vercel / Netlify functions | Same idea; a single function file |
| Fly.io / Render | Runs the Python app unmodified, closest to the systemd setup |

Whatever you choose, the contract is unchanged: **public HTTPS URL, accepts
POST, returns non-5xx.**

---

## Payload shapes

Apollo uses **two different shapes**, and the code handles both — worth knowing
if you reimplement the receiver elsewhere.

Polled from `/webhook_result/{request_id}`:

```json
{ "request_id": "…", "webhook_status": "success",
  "webhook_result": { "people": [ { "id": "…", "phone_numbers": [ … ] } ] } }
```

Pushed to the webhook — people at the **top level**, no `webhook_status`, and
often no `request_id` at all:

```json
{ "status": "success", "credits_consumed": 10,
  "people": [ { "id": "…", "phone_numbers": [ … ] } ] }
```

A failure looks like this, and is the reason phone numbers can silently come
back empty:

```json
{ "status": "failed", "credits_consumed": 0,
  "failure_reason": "you ran out of mobile number credits",
  "people": [ { "id": "…", "phone_numbers": [] } ] }
```

`apollo.extract_people()` and `apollo.extract_failure()` normalise both shapes.
Reuse them rather than writing the parsing again.

---

## Operational notes

- **Retention.** Payloads in `DATA_DIR/webhooks/` contain real phone numbers.
  Prune them; they are only useful until the enrichment run that produced them
  has finished.
- **Logs.** `DATA_DIR/logs/webhook.log` records every delivery — people count,
  how many carried phones, and any `failure_reason`. Rotating at 2MB.
- **Authentication.** There is none. Apollo does not sign these requests, so
  anyone who learns the URL can post junk to it. Consequences are limited —
  worst case, junk files in `webhooks/` — because phones are merged by matching
  Apollo person IDs the tool already holds. If that still bothers you, put a
  random path segment on the URL and treat it as a shared secret.
- **Availability.** Only matters while an enrichment is running. If it is down,
  the pre-flight refuses to start and no credits are spent.
