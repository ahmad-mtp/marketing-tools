"""Standalone, public-facing receiver for Apollo's async phone delivery.

Deliberately separate from the dashboard app. The tunnel that Apollo must be
able to reach exposes ONLY this, so /api/scrape, /api/connect and the rest of
the dashboard never become internet-reachable.

It is a dead drop: it accepts a POST, writes the payload to
data/webhooks/, and returns 200. The dashboard merges phones by polling
Apollo directly, and also sweeps this directory.
"""
import json
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .logs import get_logger

log = get_logger("webhook")

INBOX = config.DATA_DIR / "webhooks"
INBOX.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Apollo webhook receiver")


@app.get("/")
async def health():
    return JSONResponse({"ok": True, "service": "apollo-webhook-receiver"})


@app.post("/api/apollo/webhook")
async def receive(request: Request):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "expected JSON"}, status_code=400)

    rid = str(payload.get("request_id") or "unknown")
    safe = "".join(ch for ch in rid if ch.isalnum() or ch in "-_")[:64] or "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = INBOX / f"{safe}-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    from . import apollo
    people = apollo.extract_people(payload)
    failure = apollo.extract_failure(payload)
    with_phones = sum(1 for p in people if (p.get("phone_numbers") or []))
    log.info("received %d people, %d with phones, failure=%r -> %s",
             len(people), with_phones, failure, path.name)
    if failure:
        log.warning("apollo reported: %s", failure)
    return JSONResponse({"received": len(people), "with_phones": with_phones,
                         "failure": failure, "stored": path.name})
