"""Apollo client checks against a mock transport - no real API calls, no credits."""
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from app import apollo, config

# Pin config so these tests never depend on whatever is in the live .env.
config.APOLLO_BATCH_DELAY_MS = 0
config.APOLLO_REVEAL_PHONE = False
config.APOLLO_WEBHOOK_URL = ""
seen_batches: list[int] = []


def person(i):
    return {
        "id": f"ap{i}", "name": f"Person {i}", "title": "VP Marketing",
        "email": f"p{i}@acme.com", "email_status": "verified",
        "linkedin_url": f"https://www.linkedin.com/in/person-{i}/",
        "city": "Austin", "state": "TX", "country": "USA",
        "seniority": "vp", "departments": ["marketing"],
        "phone_numbers": [{"sanitized_number": f"+1512555{i:04d}"}],
        "organization": {"name": "Acme Corp", "primary_domain": "acme.com",
                          "estimated_num_employees": 250, "industry": "software"},
    }


def make_client(mode="ok"):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        body = json.loads(request.content)
        details = body["details"]
        seen_batches.append(len(details))
        if mode == "unauthorized":
            return httpx.Response(401, json={"error": "bad key"})
        if mode == "ratelimit" and state["calls"] == 1:
            return httpx.Response(429, json={"error": "slow down"})
        matches = []
        for d in details:
            idx = int(d["linkedin_url"].rstrip("/").split("-")[-1])
            matches.append(None if idx == 3 else person(idx))   # person-3 never matches
        return httpx.Response(200, json={"matches": matches, "credits_consumed": len(details),
                                          "status": "success"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.apollo.io", headers={"x-api-key": "test"})


ROWS = [{"name": f"Person {i}", "profile_url": f"https://www.linkedin.com/in/person-{i}/",
         "slug": f"person-{i}"} for i in range(25)]


async def main() -> int:
    fails = []

    # 1. happy path
    seen_batches.clear()
    async with make_client() as c:
        r = await apollo.enrich(ROWS, client=c)
    print(f"happy path -> {r['matched']} matched / {r['total']}, "
          f"{r['missed']} missed, {r['credits']} credits, batches={seen_batches}")
    if r["total"] != 25: fails.append(f"total {r['total']}")
    if r["matched"] != 24: fails.append(f"expected 24 matched, got {r['matched']}")
    if max(seen_batches) > 10: fails.append(f"batch exceeded Apollo's limit of 10: {seen_batches}")
    if seen_batches != [10, 10, 5]: fails.append(f"unexpected batching {seen_batches}")

    row0 = r["rows"][0]
    for k, want in {"apollo_status": "matched", "apollo_email": "p0@acme.com",
                    "apollo_company": "Acme Corp", "apollo_domain": "acme.com",
                    "apollo_company_size": "250", "apollo_seniority": "vp",
                    "apollo_departments": "marketing", "apollo_location": "Austin, TX, USA",
                    "apollo_phone": "+15125550000"}.items():
        if row0.get(k) != want: fails.append(f"{k}: expected {want!r}, got {row0.get(k)!r}")
    if row0.get("name") != "Person 0": fails.append("original scrape fields were lost")

    miss = next(x for x in r["rows"] if x["slug"] == "person-3")
    if miss["apollo_status"] != "no_match": fails.append(f"miss status {miss['apollo_status']!r}")
    if miss["apollo_email"] != "": fails.append("missed row should have empty email")

    # 2. bad key surfaces clearly
    async with make_client("unauthorized") as c:
        try:
            await apollo.enrich(ROWS[:3], client=c); fails.append("401 did not raise")
        except apollo.ApolloError as e:
            print("bad key    ->", str(e)[:70])
            if "API key" not in str(e): fails.append(f"unclear 401 message: {e}")

    # 3. 429 is retried
    seen_batches.clear()
    async with make_client("ratelimit") as c:
        r2 = await apollo.enrich(ROWS[:5], client=c)
    print(f"rate limit -> retried, {r2['matched']} matched, attempts={len(seen_batches)}")
    # rows[:5] contains person-3, which the mock never matches, so 4 is correct
    if r2["matched"] != 4: fails.append(f"429 retry: expected 4 matched, got {r2['matched']}")
    if len(seen_batches) != 2: fails.append(f"expected 1 retry, saw {len(seen_batches)} attempts")

    # 4. stop marks the rest skipped
    async with make_client() as c:
        r3 = await apollo.enrich(ROWS, client=c, should_stop=lambda: True)
    skipped = [x for x in r3["rows"] if x["apollo_status"] == "skipped"]
    print(f"stop       -> {len(skipped)} rows marked skipped")
    if len(skipped) != 25: fails.append(f"expected all skipped, got {len(skipped)}")

    for f in fails: print("FAIL:", f)
    print("PASS" if not fails else "FAILED")
    return 1 if fails else 0




# --- phone reveal: async delivery via polling ----------------------------
async def phone_flow() -> int:
    fails = []
    config.APOLLO_REVEAL_PHONE = True
    config.APOLLO_WEBHOOK_URL = "https://example.test/api/apollo/webhook"
    config.APOLLO_PHONE_POLL_INTERVAL_S = 0
    config.APOLLO_PHONE_POLL_TIMEOUT_S = 5
    calls = {"bulk": 0, "poll": 0}
    saw_webhook_url = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bulk_match"):
            calls["bulk"] += 1
            body = json.loads(request.content)
            saw_webhook_url.append(body.get("webhook_url"))
            # phones are NOT in the sync response
            matches = [person(int(d["linkedin_url"].rstrip("/").split("-")[-1]))
                       for d in body["details"]]
            for m in matches:
                m.pop("phone_numbers", None)
            return httpx.Response(200, json={
                "matches": matches, "credits_consumed": len(matches),
                "phone_enrichment": {"status": "pending", "request_id": "999"}})
        # webhook_result polling: pending twice, then the phones
        calls["poll"] += 1
        if calls["poll"] < 3:
            return httpx.Response(404, json={"error_code": "result_pending",
                                             "retry_after_seconds": 0})
        return httpx.Response(200, json={"request_id": "999", "webhook_status": "success",
            "webhook_result": {"people": [
                {"id": f"ap{i}", "phone_numbers": [
                    {"raw_number": f"+1 512-555-{i:04d}", "sanitized_number": f"+1512555{i:04d}",
                     "confidence_cd": "high", "status_cd": "valid_number"}]}
                for i in range(3)]}})

    c = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                          base_url="https://api.apollo.io", headers={"x-api-key": "t"})
    async with c:
        r = await apollo.enrich(ROWS[:3], client=c)
    print(f"phone flow -> matched={r['matched']} phones={r['phones']} "
          f"(bulk calls={calls['bulk']}, polls={calls['poll']})")
    if r.get("phones") != 3: fails.append(f"expected 3 phones, got {r.get('phones')}")
    if calls["poll"] < 3: fails.append("did not retry while result_pending")
    if saw_webhook_url[0] != config.APOLLO_WEBHOOK_URL:
        fails.append(f"webhook_url not sent: {saw_webhook_url}")
    row0 = r["rows"][0]
    for k, want in {"apollo_phone": "+15125550000", "apollo_phone_status": "valid_number",
                    "apollo_phone_confidence": "high"}.items():
        if row0.get(k) != want: fails.append(f"{k}: expected {want!r}, got {row0.get(k)!r}")

    # phone reveal without a webhook_url must fail loudly, not silently
    config.APOLLO_WEBHOOK_URL = ""
    async with make_client() as c2:
        try:
            await apollo.enrich(ROWS[:2], client=c2)
            fails.append("missing webhook_url did not raise")
        except apollo.ApolloError as e:
            print("no webhook ->", str(e)[:78])
            if "APOLLO_WEBHOOK_URL" not in str(e): fails.append(f"unclear message: {e}")
    config.APOLLO_REVEAL_PHONE = False

    for f in fails: print("FAIL:", f)
    print("PHONE PASS" if not fails else "PHONE FAILED")
    return 1 if fails else 0




# --- payload shapes + failure surfacing ---------------------------------
def shapes_and_failures() -> int:
    fails = []
    nested = {"webhook_result": {"people": [{"id": "a", "phone_numbers": [{"sanitized_number": "+1"}]}]}}
    flat = {"people": [{"id": "b", "phone_numbers": []}], "status": "failed",
            "failure_reason": "you ran out of mobile number credits"}
    empty = {"people": []}

    if len(apollo.extract_people(nested)) != 1: fails.append("nested people not found")
    if len(apollo.extract_people(flat)) != 1: fails.append("top-level people not found")
    if apollo.extract_people(empty) != []: fails.append("empty payload mishandled")
    if apollo.extract_people(None) != []: fails.append("None payload mishandled")

    got = apollo.extract_failure(flat)
    if got != "you ran out of mobile number credits":
        fails.append(f"failure_reason not surfaced: {got!r}")
    if apollo.extract_failure(nested) is not None:
        fails.append("false failure on a healthy payload")
    if apollo.extract_failure({"webhook_result": {"status": "failed"}}) is None:
        fails.append("nested failed status missed")

    print("shapes -> nested ok, top-level ok, failure:", repr(got))
    for f in fails: print("FAIL:", f)
    print("SHAPES PASS" if not fails else "SHAPES FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    rc = asyncio.run(main()) or asyncio.run(phone_flow()) or shapes_and_failures()
    sys.exit(rc)
