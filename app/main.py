"""FastAPI backend for the attendee harvester.

Single-session by design: one running instance drives one Chrome for one
operative. Scrapes run as a background task so the HTTP request never blocks;
the dashboard polls /api/status.
"""
import asyncio
import csv
import io
import hashlib
import json
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from . import apollo, config, profiles, scraper
from .logs import get_logger

log = get_logger("app")
from .browser import session

STATIC = Path(__file__).parent / "static"

STATE: dict = {
    "running": False,
    "count": 0,
    "rounds": 0,
    "message": "Idle.",
    "error": None,
    "started_at": None,
    "finished_at": None,
    "rows": [],
    "event_name": "",
    "event_url": "",
}


ENRICH_STATE: dict = {
    "running": False, "index": 0, "total": 0, "matched": 0, "credits": 0, "phones": 0,
    "message": "Idle.", "error": None, "stop": False, "warning": None,
}

PROFILE_STATE: dict = {
    "running": False, "index": 0, "total": 0, "opened": 0, "failed": 0,
    "message": "Idle.", "error": None, "current": None, "stop": False,
}


def _save_rows() -> None:
    """Rows lived only in memory, so a restart between phase 1 and phase 2
    lost the whole scrape. Persist them."""
    try:
        config.LAST_RUN_FILE.write_text(json.dumps({
            "event_name": STATE["event_name"], "event_url": STATE["event_url"],
            "rows": STATE["rows"]}, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_rows() -> None:
    try:
        if config.LAST_RUN_FILE.exists():
            data = json.loads(config.LAST_RUN_FILE.read_text(encoding="utf-8"))
            STATE["rows"] = data.get("rows", [])
            STATE["event_name"] = data.get("event_name", "")
            STATE["event_url"] = data.get("event_url", "")
            if STATE["rows"]:
                STATE["count"] = len(STATE["rows"])
                STATE["message"] = f"Loaded {len(STATE['rows'])} rows from the last run."
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_rows()
    await session.startup()
    try:
        yield
    finally:
        await session.shutdown()


app = FastAPI(title="LinkedIn Event Attendee Harvester", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
async def status():
    st = await session.status()
    return JSONResponse({"session": st,
                         "scrape": {k: v for k, v in STATE.items() if k != "rows"},
                         "profiles": PROFILE_STATE,
                         "enrich": {**ENRICH_STATE, "has_key": bool(config.APOLLO_API_KEY)},
                         "enriched": any(r.get("apollo_status") for r in STATE["rows"]),
                         "have_results": bool(STATE["rows"])})


@app.post("/api/connect")
async def connect():
    st = await session.connect()
    return JSONResponse({"session": st})


@app.post("/api/disconnect")
async def disconnect():
    await session.disconnect()
    return JSONResponse({"session": await session.status()})


async def _run_scrape(page, event_name: str, event_url: str):
    def on_progress(kw):
        STATE.update({k: v for k, v in kw.items() if k in ("count", "rounds", "message")})
    try:
        rows = await scraper.harvest(page, on_progress=on_progress,
                                     event_name=event_name, event_url=event_url)
        STATE["rows"] = rows
        STATE["count"] = len(rows)
        STATE["message"] = f"Done. {len(rows)} attendees captured."
        _save_rows()
    except Exception as exc:
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        STATE["message"] = "Scrape failed."
    finally:
        STATE["running"] = False
        STATE["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.post("/api/scrape")
async def scrape():
    if STATE["running"]:
        raise HTTPException(409, "A scrape is already running.")
    st = await session.status()
    if not st["connected"]:
        raise HTTPException(400, "Not connected to Chrome.")
    page = await session.event_page()
    if page is None:
        raise HTTPException(400, "No LinkedIn event tab is open in that Chrome window.")

    STATE.update({"running": True, "count": 0, "rounds": 0, "error": None, "rows": [],
                  "message": "Starting…", "finished_at": None,
                  "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "event_name": st.get("event_name") or "", "event_url": st.get("event_url") or ""})
    asyncio.create_task(_run_scrape(page, STATE["event_name"], STATE["event_url"]))
    return JSONResponse({"started": True})


@app.get("/api/results")
async def results(limit: int = 25):
    return JSONResponse({"count": len(STATE["rows"]), "rows": STATE["rows"][:limit]})


@app.get("/api/export.csv")
async def export_csv(full: bool = False):
    name, text = _csv_bytes(full=full)
    return Response(
        content=text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# The deliverable is these five columns. Apollo's values win where present -
# its title is materially cleaner than a LinkedIn headline.
EXPORT_FIELDS = ["name", "company", "title", "email", "phone"]


def _shape(row: dict) -> dict:
    return {
        "name": row.get("name", ""),
        "company": row.get("apollo_company") or row.get("company", ""),
        "title": row.get("apollo_title") or row.get("title", ""),
        "email": row.get("apollo_email", ""),
        "phone": row.get("apollo_phone", ""),
    }


def _csv_bytes(full: bool = False) -> tuple[str, str]:
    """Returns (filename, csv text) for the current rows."""
    rows = STATE["rows"]
    if not rows:
        raise HTTPException(404, "Nothing scraped yet.")
    if full:
        fields = list(scraper.FIELDS)
        if any(r.get("apollo_status") for r in rows):
            fields += apollo.FIELDS
        out = rows
    else:
        fields, out = EXPORT_FIELDS, [_shape(r) for r in rows]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader(); w.writerows(out)
    stem = re.sub(r"[^A-Za-z0-9]+", "-", STATE["event_name"] or "linkedin-event").strip("-").lower()
    name = f"{stem or 'linkedin-event'}-attendees-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return name, buf.getvalue()


@app.post("/api/export-file")
async def export_file(full: bool = False):
    """Write the CSV straight to disk.

    The dashboard is often viewed inside the automation-controlled Chrome,
    which silently drops normal downloads - this path does not depend on the
    browser at all.
    """
    name, text = _csv_bytes(full=full)
    out_dir = config.EXPORT_DIR
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
    except Exception as exc:
        raise HTTPException(500, f"Could not write to {out_dir}: {exc}")
    return JSONResponse({"path": str(path), "rows": len(STATE["rows"]),
                         "bytes": len(text.encode("utf-8"))})


@app.post("/api/dump")
async def dump():
    """Snapshot the live DOM so selectors can be tuned against a real page."""
    page = await session.event_page()
    if page is None:
        raise HTTPException(400, "No LinkedIn event tab is open.")
    html = await page.content()
    path = config.DEBUG_DIR / f"event-{datetime.now().strftime('%Y%m%d-%H%M%S')}.html"
    path.write_text(html, encoding="utf-8")
    probe = await page.evaluate(
        "() => ({ profileLinks: document.querySelectorAll('a[href*=\"/in/\"]').length,"
        " dialogs: document.querySelectorAll('[role=\"dialog\"]').length })")
    return JSONResponse({"saved": str(path), "bytes": len(html), **probe})


class OpenEvent(BaseModel):
    url: str


@app.post("/api/open-event")
async def open_event(payload: OpenEvent):
    """Drive the browser straight to the event/attendees URL, rather than
    asking the operative to navigate and then guessing which tab they meant."""
    url = payload.url.strip()
    if not url:
        raise HTTPException(400, "No URL given.")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    if not re.search(r"//([a-z0-9-]+\.)*linkedin\.com/", url, re.I):
        raise HTTPException(400, "That doesn't look like a LinkedIn URL.")
    try:
        await session.goto(url)
    except Exception as exc:
        raise HTTPException(400, f"Could not open that URL: {type(exc).__name__}: {exc}")
    return JSONResponse({"session": await session.status()})


class OpenProfiles(BaseModel):
    mode: str = "visit"            # "visit" (one reused tab) or "tabs"
    limit: int | None = None
    delay_min_ms: int | None = None
    delay_max_ms: int | None = None


async def _run_profiles(ctx, rows, body: OpenProfiles):
    def on_progress(kw):
        PROFILE_STATE.update({k: v for k, v in kw.items()
                              if k in ("index", "total", "opened", "failed", "current", "message")})
    try:
        result = await profiles.visit_profiles(
            ctx, rows, mode=body.mode,
            delay_min_ms=body.delay_min_ms, delay_max_ms=body.delay_max_ms,
            limit=body.limit, should_stop=lambda: PROFILE_STATE["stop"],
            on_progress=on_progress)
        if not PROFILE_STATE["stop"]:
            PROFILE_STATE["message"] = (
                f"Done. Opened {result['opened']} of {result['total']}"
                + (f", {result['failed']} failed." if result["failed"] else "."))
    except Exception as exc:
        PROFILE_STATE["error"] = f"{type(exc).__name__}: {exc}"
        PROFILE_STATE["message"] = "Opening profiles failed."
    finally:
        PROFILE_STATE["running"] = False
        PROFILE_STATE["stop"] = False


@app.post("/api/open-profiles")
async def open_profiles(body: OpenProfiles):
    if PROFILE_STATE["running"]:
        raise HTTPException(409, "Already opening profiles.")
    if STATE["running"]:
        raise HTTPException(409, "A scrape is still running.")
    if not STATE["rows"]:
        raise HTTPException(400, "Nothing scraped yet - run a scrape first.")
    if body.mode not in ("visit", "tabs"):
        raise HTTPException(400, "mode must be 'visit' or 'tabs'.")
    try:
        ctx = session.context
    except Exception as exc:
        raise HTTPException(400, str(exc))

    PROFILE_STATE.update({"running": True, "stop": False, "index": 0, "opened": 0,
                          "failed": 0, "error": None, "current": None,
                          "total": len(STATE["rows"]), "message": "Starting…"})
    asyncio.create_task(_run_profiles(ctx, list(STATE["rows"]), body))
    return JSONResponse({"started": True})


@app.post("/api/stop-profiles")
async def stop_profiles():
    if not PROFILE_STATE["running"]:
        raise HTTPException(400, "Not running.")
    PROFILE_STATE["stop"] = True
    PROFILE_STATE["message"] = "Stopping after the current profile…"
    return JSONResponse({"stopping": True})


async def _run_enrich(targets: list[dict]):
    def on_progress(kw):
        ENRICH_STATE.update({k: v for k, v in kw.items()
                             if k in ("index", "total", "matched", "credits", "message")})
    try:
        result = await apollo.enrich(targets,
                                     should_stop=lambda: ENRICH_STATE["stop"],
                                     on_progress=on_progress)
        # Merge enriched rows back by slug, leaving untouched rows alone.
        enriched = {r.get("slug"): r for r in result["rows"] if r.get("slug")}
        STATE["rows"] = [enriched.get(r.get("slug"), r) for r in STATE["rows"]]
        _save_rows()
        ENRICH_STATE["phones"] = result.get("phones", 0)
        failures = result.get("failures") or []
        # A phone failure is not an error - emails still landed - but it must
        # not read as "Apollo had no phone data".
        ENRICH_STATE["warning"] = "; ".join(failures) if failures else None
        if failures:
            log.warning("enrichment completed with failures: %s", failures)
        ENRICH_STATE["message"] = (
            f"Done. {result['matched']} of {result['total']} matched, "
            f"{result.get('phones', 0)} with phone numbers, "
            f"{result['missed']} missed, {result['credits']} credits used.")
    except Exception as exc:
        ENRICH_STATE["error"] = f"{type(exc).__name__}: {exc}"
        ENRICH_STATE["message"] = "Enrichment failed."
    finally:
        ENRICH_STATE["running"] = False
        ENRICH_STATE["stop"] = False


class EnrichOpts(BaseModel):
    limit: int | None = None       # enrich only the first N rows - credit brake
    only_new: bool = True          # skip anyone Apollo has already answered for
    retry_missed: bool = False     # include previous no_match rows in a retry


@app.post("/api/enrich")
async def enrich(body: EnrichOpts | None = None):
    if ENRICH_STATE["running"]:
        raise HTTPException(409, "Enrichment already running.")
    if STATE["running"]:
        raise HTTPException(409, "A scrape is still running.")
    if not STATE["rows"]:
        raise HTTPException(400, "Nothing scraped yet - run a scrape first.")
    if not config.APOLLO_API_KEY:
        raise HTTPException(400, "No APOLLO_API_KEY set. Add it to .env and restart the server.")

    # Guard the credits: a dead tunnel would otherwise burn a run for nothing.
    if config.APOLLO_REVEAL_PHONE:
        ok, detail = await apollo.check_webhook_reachable(config.APOLLO_WEBHOOK_URL)
        if not ok:
            raise HTTPException(
                400, f"Webhook URL not reachable ({detail}). Apollo requires a live "
                     "webhook_url for phone reveal - restart the tunnel and update "
                     "APOLLO_WEBHOOK_URL, or set APOLLO_REVEAL_PHONE=false.")

    only_new = body.only_new if body else True
    retry_missed = body.retry_missed if body else False

    def needs_enrichment(row: dict) -> bool:
        status = row.get("apollo_status") or ""
        if not status:
            return True
        # A previous no_match can be worth retrying; a match never is.
        return retry_missed and status.startswith("no_match")

    pool = [r for r in STATE["rows"] if needs_enrichment(r)] if only_new else list(STATE["rows"])
    if not pool:
        raise HTTPException(400, "Every row has already been enriched. "
                                 "Send only_new=false to run them again.")

    limit = body.limit if body else None
    targets = pool[:limit] if limit and limit > 0 else pool

    ENRICH_STATE.update({"running": True, "stop": False, "index": 0, "matched": 0,
                         "phones": 0, "credits": 0, "error": None, "warning": None,
                         "total": len(targets), "message": "Starting\u2026"})
    log.info("enrich requested: targets=%d of %d rows (only_new=%s retry_missed=%s limit=%s) "
             "reveal_email=%s reveal_phone=%s",
             len(targets), len(STATE["rows"]), only_new, retry_missed, limit,
             config.APOLLO_REVEAL_PERSONAL_EMAILS, config.APOLLO_REVEAL_PHONE)
    asyncio.create_task(_run_enrich(targets))
    return JSONResponse({"started": True, "enriching": len(targets)})


@app.post("/api/stop-enrich")
async def stop_enrich():
    if not ENRICH_STATE["running"]:
        raise HTTPException(400, "Not running.")
    ENRICH_STATE["stop"] = True
    ENRICH_STATE["message"] = "Stopping after the current batch\u2026"
    return JSONResponse({"stopping": True})


def _sweep_webhook_inbox() -> int:
    """Merge anything the public receiver dropped in. Polling is the primary
    path; this catches results that arrived by push."""
    inbox = config.DATA_DIR / "webhooks"
    if not inbox.exists():
        return 0
    by_id: dict = {}
    for f in sorted(inbox.glob("*.json")):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        failure = apollo.extract_failure(payload)
        if failure:
            log.warning("webhook inbox file %s reported: %s", f.name, failure)
            ENRICH_STATE["warning"] = failure
        for person in apollo.extract_people(payload):
            if person.get("id"):
                by_id[person["id"]] = apollo._phone_fields(person)
    updated = 0
    for row in STATE["rows"]:
        pid = row.get("apollo_id")
        if pid and pid in by_id and by_id[pid].get("apollo_phone"):
            row.update(by_id[pid]); updated += 1
    if updated:
        _save_rows()
    return updated


@app.post("/api/sweep-webhooks")
async def sweep_webhooks():
    n = _sweep_webhook_inbox()
    ENRICH_STATE["phones"] = ENRICH_STATE.get("phones", 0) + n
    ENRICH_STATE["message"] = f"Swept webhook inbox: {n} phone numbers merged."
    return JSONResponse({"merged": n})


@app.post("/api/apollo/webhook")
async def apollo_webhook(payload: dict):
    """Optional receiver for Apollo's asynchronous phone delivery.

    Enrichment does not depend on this - it polls /webhook_result/{request_id}
    instead, which needs no public address. This exists so that if you do
    expose the app (ngrok/cloudflared) and point APOLLO_WEBHOOK_URL here,
    pushed results are merged too.
    """
    people = apollo.extract_people(payload)
    failure = apollo.extract_failure(payload)
    if failure:
        log.warning("apollo webhook reported failure: %s", failure)
        ENRICH_STATE["warning"] = failure
    by_id = {}
    for person in people:
        pid = person.get("id")
        if pid:
            by_id[pid] = apollo._phone_fields(person)

    updated = 0
    for row in STATE["rows"]:
        pid = row.get("apollo_id")
        if pid and pid in by_id and by_id[pid].get("apollo_phone"):
            row.update(by_id[pid])
            updated += 1
    if updated:
        _save_rows()
        ENRICH_STATE["phones"] = ENRICH_STATE.get("phones", 0) + updated
        ENRICH_STATE["message"] = f"Webhook delivered {updated} phone numbers."
    return JSONResponse({"received": len(people), "updated": updated})


@app.post("/api/clear")
async def clear_rows():
    """Reset the working set - handy before a fresh run."""
    STATE["rows"] = []
    STATE["count"] = 0
    STATE["event_name"] = STATE["event_url"] = ""
    STATE["message"] = "Cleared."
    for k in ("index", "total", "matched", "phones", "credits"):
        ENRICH_STATE[k] = 0
    ENRICH_STATE["message"] = "Idle."
    ENRICH_STATE["warning"] = ENRICH_STATE["error"] = None
    try:
        config.LAST_RUN_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    log.info("working set cleared")
    return JSONResponse({"cleared": True})
