"""Apollo people-enrichment client.

POST {base}/api/v1/people/bulk_match, authenticated with an `x-api-key`
header, matching on linkedin_url. Apollo caps a bulk request at 10 records,
so rows are sent in batches with a pause between them.

This never touches LinkedIn, so it is the cheap path: minutes instead of the
hours a browser walk of the same list would take.
"""
import asyncio
from typing import Callable, Iterable, Optional

import httpx

from . import config
from .logs import get_logger

log = get_logger("apollo")

BULK_PATH = "/api/v1/people/bulk_match"
WEBHOOK_RESULT_PATH = "/api/v1/webhook_result/{request_id}"

# Columns appended to the CSV. apollo_status says what happened per row, so a
# blank email is distinguishable from a row that was never sent.
FIELDS = [
    "apollo_status", "apollo_email", "apollo_email_status",
    "apollo_phone", "apollo_phone_status", "apollo_phone_confidence", "apollo_phones",
    "apollo_title", "apollo_seniority", "apollo_departments",
    "apollo_company", "apollo_domain", "apollo_company_size", "apollo_industry",
    "apollo_location", "apollo_linkedin_url", "apollo_id",
]


class ApolloError(RuntimeError):
    pass


# Reasons a phone enrichment came back empty, collected during a run so the
# dashboard can show "out of mobile number credits" instead of a silent zero.
_FAILURES: list[str] = []


def _blank(status: str) -> dict:
    row = {f: "" for f in FIELDS}
    row["apollo_status"] = status
    return row


def _flatten(person: Optional[dict]) -> dict:
    if not person:
        return _blank("no_match")
    org = person.get("organization") or {}
    contact = person.get("contact") or {}

    phones = person.get("phone_numbers") or contact.get("phone_numbers") or []
    phone = ""
    if isinstance(phones, list) and phones:
        first = phones[0]
        phone = first.get("sanitized_number") or first.get("raw_number") or "" \
            if isinstance(first, dict) else str(first)

    loc = ", ".join(x for x in (person.get("city"), person.get("state"),
                                person.get("country")) if x)
    depts = person.get("departments") or []

    return {
        "apollo_status": "matched",
        "apollo_email": person.get("email") or contact.get("email") or "",
        "apollo_email_status": person.get("email_status") or "",
        "apollo_phone": phone,
        "apollo_title": person.get("title") or "",
        "apollo_seniority": person.get("seniority") or "",
        "apollo_departments": ", ".join(depts) if isinstance(depts, list) else str(depts),
        "apollo_company": org.get("name") or "",
        "apollo_domain": org.get("primary_domain") or org.get("website_url") or "",
        "apollo_company_size": str(org.get("estimated_num_employees") or ""),
        "apollo_industry": org.get("industry") or "",
        "apollo_location": loc,
        "apollo_linkedin_url": person.get("linkedin_url") or "",
        "apollo_id": person.get("id") or "",
    }


def _chunks(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


async def _post_batch(client: httpx.AsyncClient, urls: list[str]) -> tuple[list, dict]:
    payload = {
        "details": [{"linkedin_url": u} for u in urls],
        "reveal_personal_emails": config.APOLLO_REVEAL_PERSONAL_EMAILS,
        "reveal_phone_number": config.APOLLO_REVEAL_PHONE,
    }
    # Apollo rejects the call outright if phones are requested without this.
    if config.APOLLO_REVEAL_PHONE:
        if not config.APOLLO_WEBHOOK_URL:
            raise ApolloError(
                "APOLLO_REVEAL_PHONE=true requires APOLLO_WEBHOOK_URL in .env - "
                "Apollo makes it mandatory for phone reveal.")
        payload["webhook_url"] = config.APOLLO_WEBHOOK_URL
    delay = 2.0
    last = None
    for attempt in range(1, config.APOLLO_MAX_RETRIES + 1):
        try:
            r = await client.post(BULK_PATH, json=payload)
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if r.status_code == 200:
                body = r.json()
                pe = body.get("phone_enrichment") or {}
                failure = extract_failure(body) or extract_failure(pe)
                log.info("bulk_match n=%d -> matches=%d credits=%s phone_status=%r "
                         "request_id=%s failure=%r",
                         len(urls), len(body.get("matches") or []),
                         body.get("credits_consumed"), pe.get("status"),
                         _request_id(body), failure)
                if failure:
                    _FAILURES.append(failure)
                return (body.get("matches") or []), body
            if r.status_code in (401, 403):
                raise ApolloError(f"Apollo rejected the API key ({r.status_code}). "
                                  "Check APOLLO_API_KEY in .env.")
            if r.status_code == 422:
                raise ApolloError(f"Apollo rejected the request (422): {r.text[:200]}")
            # 429 and 5xx are worth retrying.
            last = f"HTTP {r.status_code}: {r.text[:160]}"
            if r.status_code not in (429,) and r.status_code < 500:
                raise ApolloError(last)
        if attempt < config.APOLLO_MAX_RETRIES:
            await asyncio.sleep(delay)
            delay *= 2
    raise ApolloError(f"Apollo failed after {config.APOLLO_MAX_RETRIES} attempts: {last}")


def extract_people(payload: dict) -> list:
    """Apollo returns people under webhook_result.people when polled, but at
    the top level in the pushed webhook. Accept either."""
    if not isinstance(payload, dict):
        return []
    nested = (payload.get("webhook_result") or {})
    if isinstance(nested, dict) and nested.get("people"):
        return nested["people"] or []
    return payload.get("people") or []


def extract_failure(payload: dict) -> Optional[str]:
    """Surface the reason a phone enrichment produced nothing. Apollo reports
    this as status=failed + failure_reason, in either payload shape."""
    if not isinstance(payload, dict):
        return None
    for scope in (payload, payload.get("webhook_result") or {}):
        if not isinstance(scope, dict):
            continue
        reason = scope.get("failure_reason")
        status = str(scope.get("status") or scope.get("webhook_status") or "").lower()
        if reason:
            return str(reason)
        if status in ("failed", "error"):
            return f"phone enrichment {status}"
    return None


def _request_id(body: dict):
    """Phone results are keyed by this. Apollo puts it top level and also
    inside phone_enrichment."""
    return (body.get("request_id")
            or (body.get("phone_enrichment") or {}).get("request_id"))


def _phone_fields(person: dict) -> dict:
    phones = person.get("phone_numbers") or []
    if not isinstance(phones, list) or not phones:
        return {}
    first = phones[0] if isinstance(phones[0], dict) else {"raw_number": str(phones[0])}
    every = [p.get("sanitized_number") or p.get("raw_number") or ""
             for p in phones if isinstance(p, dict)]
    return {
        "apollo_phone": first.get("sanitized_number") or first.get("raw_number") or "",
        "apollo_phone_status": first.get("status_cd") or "",
        "apollo_phone_confidence": first.get("confidence_cd") or "",
        "apollo_phones": ", ".join(x for x in every if x),
    }


async def poll_phone_result(client: httpx.AsyncClient, request_id: str,
                            should_stop: Optional[Callable[[], bool]] = None,
                            on_progress: Optional[Callable[[dict], None]] = None) -> dict:
    """Phones arrive asynchronously. Rather than needing a publicly reachable
    webhook, poll GET /webhook_result/{request_id}: 404 + result_pending while
    Apollo works, 200 with webhook_result.people[] when done.

    Returns {person_id: phone_fields}.
    """
    path = WEBHOOK_RESULT_PATH.format(request_id=request_id)
    waited = 0.0
    while waited < config.APOLLO_PHONE_POLL_TIMEOUT_S:
        if should_stop and should_stop():
            return {}
        try:
            r = await client.get(path)
        except httpx.HTTPError:
            r = None
        if r is not None and r.status_code == 200:
            body = r.json()
            failure = extract_failure(body)
            people = extract_people(body)
            found = sum(1 for p in people if (p.get("phone_numbers") or []))
            log.info("poll %s -> 200, people=%d with_phones=%d failure=%r",
                     request_id, len(people), found, failure)
            if failure:
                _FAILURES.append(failure)
            out = {}
            for person in people:
                pid = person.get("id")
                if pid:
                    out[pid] = _phone_fields(person)
            return out
        wait = config.APOLLO_PHONE_POLL_INTERVAL_S
        if r is not None and r.status_code == 404:
            try:
                wait = float((r.json() or {}).get("retry_after_seconds") or wait)
            except Exception:
                pass
        elif r is not None and r.status_code in (401, 403):
            raise ApolloError(f"Apollo rejected the API key while polling ({r.status_code}).")
        if on_progress:
            on_progress(message=f"waiting on phone results ({int(waited)}s)")
        await asyncio.sleep(wait)
        waited += wait
    return {}


async def check_webhook_reachable(url: str, timeout: float = 12.0) -> tuple[bool, str]:
    """Apollo credits are scarce, so confirm the webhook address is actually
    answering before spending any. A tunnel that died silently is the failure
    this guards against."""
    if not url:
        return False, "APOLLO_WEBHOOK_URL is empty"
    base = url.rsplit("/api/", 1)[0] if "/api/" in url else url
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
            r = await c.get(base)
            if r.status_code < 500:
                return True, f"reachable (HTTP {r.status_code})"
            return False, f"returned HTTP {r.status_code}"
    except Exception as exc:
        return False, f"unreachable: {type(exc).__name__}"


async def enrich(
    rows: list[dict],
    client: Optional[httpx.AsyncClient] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    """Enrich rows in place-ish: returns a new list of merged rows plus a summary."""
    if not config.APOLLO_API_KEY and client is None:
        raise ApolloError("No APOLLO_API_KEY set. Add it to .env and restart.")
    # Check once up front rather than letting every batch fail in turn.
    if config.APOLLO_REVEAL_PHONE and not config.APOLLO_WEBHOOK_URL:
        raise ApolloError(
            "APOLLO_REVEAL_PHONE=true requires APOLLO_WEBHOOK_URL in .env - "
            "Apollo makes it mandatory for phone reveal.")

    owned = client is None
    if owned:
        client = httpx.AsyncClient(
            base_url=config.APOLLO_BASE_URL,
            timeout=config.APOLLO_TIMEOUT_S,
            headers={"x-api-key": config.APOLLO_API_KEY,
                     "Content-Type": "application/json",
                     "Cache-Control": "no-cache"},
        )

    _FAILURES.clear()
    out: list[dict] = []
    matched = credits = phones_found = 0
    total = len(rows)
    # (request_id, indices into `out`) - phones arrive later, per batch.
    pending: list[tuple[str, list[int]]] = []

    def report(**kw):
        if on_progress:
            on_progress(kw)

    try:
        done = 0
        for batch in _chunks(rows, config.APOLLO_BATCH_SIZE):
            if should_stop and should_stop():
                for r in batch:
                    out.append({**r, **_blank("skipped")})
                done += len(batch)
                report(index=done, total=total, matched=matched,
                       message=f"Stopped by user after {done} of {total}.")
                continue

            urls = [r.get("profile_url", "") for r in batch]
            try:
                matches, body = await _post_batch(client, urls)
                credits += int(body.get("credits_consumed") or 0)
            except ApolloError as exc:
                for r in batch:
                    out.append({**r, **_blank(f"error: {exc}"[:120])})
                done += len(batch)
                report(index=done, total=total, matched=matched,
                       message=f"{done}/{total} - batch failed: {exc}")
                if "API key" in str(exc):
                    raise
                continue

            first_index = len(out)
            for i, r in enumerate(batch):
                person = matches[i] if i < len(matches) else None
                flat = _flatten(person)
                if flat["apollo_status"] == "matched":
                    matched += 1
                out.append({**r, **flat})

            rid = _request_id(body)
            if config.APOLLO_REVEAL_PHONE and rid:
                pending.append((str(rid), list(range(first_index, len(out)))))

            done += len(batch)
            report(index=done, total=total, matched=matched, credits=credits,
                   message=f"{done}/{total} - {matched} matched, {credits} credits")

            if done < total and config.APOLLO_BATCH_DELAY_MS > 0:
                await asyncio.sleep(config.APOLLO_BATCH_DELAY_MS / 1000.0)
        # Phones are delivered asynchronously, so collect them only after every
        # batch has been submitted - all of them enrich in parallel that way.
        for n, (rid, idxs) in enumerate(pending, 1):
            if should_stop and should_stop():
                report(message=f"Stopped before phone results for batch {n}.")
                break
            report(message=f"Waiting on phone numbers, batch {n}/{len(pending)}\u2026")
            found = await poll_phone_result(
                client, rid, should_stop,
                on_progress=lambda **kw: report(
                    message=f"Batch {n}/{len(pending)}: {kw.get('message','')}"))
            for i in idxs:
                pid = out[i].get("apollo_id")
                if pid and pid in found and found[pid].get("apollo_phone"):
                    out[i].update(found[pid])
                    phones_found += 1
            report(message=f"Phones: {phones_found} of {matched} matched people so far")
    finally:
        if owned:
            await client.aclose()

    # De-duplicate while preserving order - the same reason repeats per batch.
    failures = list(dict.fromkeys(_FAILURES))
    result = {"rows": out, "total": total, "matched": matched,
              "missed": total - matched, "credits": credits,
              "phones": phones_found, "failures": failures}
    log.info("enrich done: total=%d matched=%d phones=%d credits=%d failures=%s",
             total, matched, phones_found, credits, failures or "none")
    return result
