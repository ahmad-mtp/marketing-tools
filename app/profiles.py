"""Phase 2 - walk the scraped profiles in the live Chrome session.

Two modes:
  visit - reuse a single tab, load each profile in turn. Safe for hundreds.
  tabs  - leave each profile open in its own tab, hard-capped, because a few
          hundred tabs will exhaust Chrome long before the list is done.

Paced with a randomised delay: back-to-back profile loads are the signature
LinkedIn rate-limits on.
"""
import asyncio
import random
from typing import Callable, Optional

from . import config


async def visit_profiles(
    context,
    rows: list[dict],
    mode: str = "visit",
    delay_min_ms: Optional[int] = None,
    delay_max_ms: Optional[int] = None,
    limit: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> dict:
    delay_min = delay_min_ms if delay_min_ms is not None else config.PROFILE_DELAY_MIN_MS
    delay_max = delay_max_ms if delay_max_ms is not None else config.PROFILE_DELAY_MAX_MS
    if delay_max < delay_min:
        delay_max = delay_min

    urls = [r["profile_url"] for r in rows if r.get("profile_url")]
    cap = min(limit or config.PROFILE_MAX_VISITS, config.PROFILE_MAX_VISITS)
    if mode == "tabs":
        cap = min(cap, config.PROFILE_MAX_TABS)
    urls = urls[:cap]
    total = len(urls)

    def report(**kw):
        if on_progress:
            on_progress(kw)

    opened, failed = 0, 0
    reusable = None
    open_tabs: list = []

    for i, url in enumerate(urls, 1):
        if should_stop and should_stop():
            report(message=f"Stopped by user after {i - 1} of {total}.")
            break
        try:
            if mode == "tabs":
                page = await context.new_page()
                open_tabs.append(page)
            else:
                if reusable is None or reusable.is_closed():
                    reusable = await context.new_page()
                page = reusable
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            opened += 1
        except Exception as exc:
            failed += 1
            report(message=f"{url.rsplit('/in/', 1)[-1].strip('/')}: {type(exc).__name__}")

        report(index=i, total=total, opened=opened, failed=failed, current=url,
               message=f"{i}/{total} - opened {opened}, failed {failed}")

        # Dwell after opening, including on the last profile: the page needs
        # to stay open long enough for anything running on it to finish.
        dwell = random.uniform(delay_min, delay_max) / 1000.0
        deadline = asyncio.get_event_loop().time() + dwell
        while asyncio.get_event_loop().time() < deadline:
            if should_stop and should_stop():
                break
            await asyncio.sleep(min(0.5, max(0.0, deadline - asyncio.get_event_loop().time())))

    if mode == "visit" and reusable is not None and not reusable.is_closed():
        try:
            await reusable.close()
        except Exception:
            pass

    return {"total": total, "opened": opened, "failed": failed,
            "tabs_left_open": len(open_tabs) if mode == "tabs" else 0}
