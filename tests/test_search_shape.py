"""Regression for the real LinkedIn people-search DOM shape.

Built from a live capture: no <li>, hashed class names, each real result
linked six times, and every card ending in a short mutual-connections block
that links to people who are NOT results. Scraping those polluted the CSV.
"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.async_api import async_playwright
from app import scraper

FIXTURE = (Path(__file__).parent / "fixture_search.html").resolve()
EXPECTED = {f"real-person-{i}" for i in range(10)}


async def main() -> int:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="chrome", headless=True)
        p = await b.new_page()
        await p.goto(FIXTURE.as_uri())
        await p.wait_for_timeout(300)
        total_links = await p.evaluate("document.querySelectorAll('a[href*=\"/in/\"]').length")
        rows = await scraper.harvest(p, event_name="Search", event_url=str(FIXTURE))
        await b.close()

    got = {r["slug"] for r in rows}
    fails = []
    if got != EXPECTED:
        if EXPECTED - got: fails.append(f"missed real results: {sorted(EXPECTED - got)}")
        if got - EXPECTED: fails.append(f"scraped non-results: {sorted(got - EXPECTED)}")
    mutual = [s for s in got if s.startswith("mutual-")]
    if mutual:
        fails.append(f"mutual connections scraped as people: {mutual[:4]}")

    first = next((r for r in rows if r["slug"] == "real-person-0"), None)
    if first:
        for k, want in {"name": "Real Person 0", "title": "Chief Product Officer",
                        "company": "Company 0", "degree": "1st"}.items():
            if first[k] != want:
                fails.append(f"{k}: expected {want!r}, got {first[k]!r}")

    print(f"{total_links} profile links on page -> {len(rows)} people extracted")
    if first:
        print("sample:", {k: first[k] for k in ("name", "title", "company", "degree")})
    for f in fails: print("FAIL:", f)
    print("PASS" if not fails else "FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
