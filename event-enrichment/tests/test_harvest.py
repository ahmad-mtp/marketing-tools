"""End-to-end check of the harvest pipeline against a local fixture that
mimics LinkedIn's DOM quirks. Proves scroll + extract + dedupe + CSV without
touching LinkedIn."""
import asyncio, csv, io, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.async_api import async_playwright
from app import scraper

FIXTURE = (Path(__file__).parent / "fixture_event.html").resolve()
EXPECTED = 60


async def main() -> int:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.goto(FIXTURE.as_uri())
        rows = await scraper.harvest(page, event_name="Fixture Event",
                                     event_url="https://www.linkedin.com/events/fixture/")
        await browser.close()

    fails = []
    if len(rows) != EXPECTED:
        fails.append(f"expected {EXPECTED} attendees, got {len(rows)}")
    if len({r['slug'] for r in rows}) != len(rows):
        fails.append("duplicate slugs survived dedupe")

    # Each fixture card also links to two "mutual connections" who are NOT
    # attendees. Real LinkedIn does the same, and scraping them pollutes the CSV.
    decoys = [r['slug'] for r in rows if r['slug'].startswith('decoy-')]
    if decoys:
        fails.append(f"{len(decoys)} mutual-connection links scraped as attendees: {decoys[:3]}")

    first = next((r for r in rows if r["slug"].startswith("sarah-taylor-0")), None)
    if not first:
        fails.append("did not find the first fixture attendee")
    else:
        checks = {"name": "Sarah Taylor", "headline": "VP Marketing at Acme Corp",
                  "title": "VP Marketing", "company": "Acme Corp", "degree": "1st",
                  "profile_url": "https://www.linkedin.com/in/sarah-taylor-0/"}
        for k, want in checks.items():
            if first[k] != want:
                fails.append(f"{k}: expected {want!r}, got {first[k]!r}")

    if any(r["name"] and r["name"] != r["name"].strip() for r in rows):
        fails.append("untrimmed names")
    doubled = [r["name"] for r in rows if len(r["name"]) > 40]
    if doubled:
        fails.append(f"doubled screen-reader text leaked into names: {doubled[:2]}")

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=scraper.FIELDS, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)
    if len(buf.getvalue().splitlines()) != len(rows) + 1:
        fails.append("CSV row count mismatch")

    print(f"harvested {len(rows)} rows")
    if first:
        print("sample:", {k: first[k] for k in ('name', 'title', 'company', 'degree')})
    for f in fails:
        print("FAIL:", f)
    print("PASS" if not fails else "FAILED")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
