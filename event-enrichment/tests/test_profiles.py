"""Phase 2 checks: pacing, limits and the stop control, against local pages
so nothing touches LinkedIn."""
import asyncio, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.async_api import async_playwright
from app import profiles

TMP = Path(__file__).parent / "_tmp_profiles"


def make_pages(n: int) -> list[dict]:
    TMP.mkdir(exist_ok=True)
    rows = []
    for i in range(n):
        f = TMP / f"p{i}.html"
        f.write_text(f"<html><body><h1>Profile {i}</h1></body></html>", encoding="utf-8")
        rows.append({"profile_url": f.resolve().as_uri(), "slug": f"person-{i}"})
    return rows


async def main() -> int:
    rows = make_pages(6)
    fails = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context()

        # 1. visit mode opens everything and reuses one tab
        before = len(ctx.pages)
        t0 = time.monotonic()
        r = await profiles.visit_profiles(ctx, rows, mode="visit",
                                          delay_min_ms=60, delay_max_ms=90)
        elapsed = time.monotonic() - t0
        print(f"visit  -> {r}  in {elapsed:.2f}s")
        if r["opened"] != 6: fails.append(f"visit opened {r['opened']}, expected 6")
        if len(ctx.pages) > before: fails.append("visit mode leaked tabs")
        if elapsed < 5 * 0.060: fails.append("delay was not applied between profiles")

        # 2. limit is honoured
        r = await profiles.visit_profiles(ctx, rows, mode="visit", limit=2,
                                          delay_min_ms=10, delay_max_ms=10)
        print(f"limit  -> {r}")
        if r["total"] != 2 or r["opened"] != 2: fails.append(f"limit ignored: {r}")

        # 3. stop halts mid-run
        seen = {"n": 0}
        def progress(kw):
            if "index" in kw: seen["n"] = kw["index"]
        r = await profiles.visit_profiles(ctx, rows, mode="visit",
                                          delay_min_ms=10, delay_max_ms=10,
                                          should_stop=lambda: seen["n"] >= 3,
                                          on_progress=progress)
        print(f"stop   -> {r}")
        if r["opened"] > 4: fails.append(f"stop did not halt: opened {r['opened']}")

        # 4. tabs mode leaves tabs open, capped
        ctx2 = await browser.new_context()
        r = await profiles.visit_profiles(ctx2, rows, mode="tabs",
                                          delay_min_ms=10, delay_max_ms=10)
        print(f"tabs   -> {r}  (open tabs: {len(ctx2.pages)})")
        if r["opened"] != 6: fails.append(f"tabs opened {r['opened']}, expected 6")
        if len(ctx2.pages) < 6: fails.append("tabs mode did not leave tabs open")

        # 5. a broken URL is counted, not fatal
        bad = rows[:2] + [{"profile_url": "file:///definitely/missing.html", "slug": "x"}]
        r = await profiles.visit_profiles(ctx, bad, mode="visit",
                                          delay_min_ms=10, delay_max_ms=10)
        print(f"broken -> {r}")
        if r["opened"] != 2 or r["failed"] != 1: fails.append(f"bad URL mishandled: {r}")

        await browser.close()

    for f in fails: print("FAIL:", f)
    print("PASS" if not fails else "FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    code = asyncio.run(main())
    import shutil; shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
