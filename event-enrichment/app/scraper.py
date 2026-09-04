"""Harvest the attendee list from an open LinkedIn event tab.

Deliberately anchored on `a[href*="/in/"]` rather than CSS class names:
profile links are the one thing on that page that cannot change without
breaking LinkedIn itself. Everything else is best-effort and is echoed back
in `raw_lines` so selectors can be tuned against a real page.
"""
import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import config

# --- in-page extraction -------------------------------------------------
EXTRACT_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();

  // LinkedIn renders each label twice: a visible <span aria-hidden="true">
  // and a screen-reader twin. Naive textContent yields "Sarah TSarah T".
  const textOf = el => {
    if (!el) return '';
    const c = el.cloneNode(true);
    c.querySelectorAll('.visually-hidden, .a11y-text, [class*="visually-hidden"]')
      .forEach(n => n.remove());
    return clean(c.textContent);
  };

  const NOISE = /^(message|connect|connected|follow|following|pending|withdraw|view\s+profile|invite|see\s+more|show\s+more|load\s+more|[·•]|\W*\d(?:st|nd|rd)\b)/i;
  const good = t => !!t && t.length <= 80 && !NOISE.test(t);

  // Attendees usually live in a modal; fall back to the whole page.
  const dialogs = [...document.querySelectorAll('[role="dialog"]')]
    .filter(d => d.offsetParent !== null && d.querySelector('a[href*="/in/"]'));
  const scope = dialogs.length ? dialogs[dialogs.length - 1] : document.body;

  // "Bob Deckard, Momin Qureshi & 2 other mutual connections" links to people
  // who are NOT on the list. That sentence is its own short element ending in
  // the phrase - a far more reliable tell than any hashed class name.
  //
  // The length cap matters: a real person's card can also END with a mutual
  // line, so only the bare sentence should match. Measured on live search
  // results, mutual blocks run 33-69 chars and real cards start at 127.
  const MUTUAL_MAX = 120;
  const inMutualBlock = a => {
    let n = a.parentElement;
    for (let i = 0; i < 3 && n; i++, n = n.parentElement) {
      const t = clean(n.textContent);
      if (/mutual connections?\s*$/i.test(t)) return t.length <= MUTUAL_MAX;
    }
    return false;
  };

  // LinkedIn search results are NOT in <li>, and every class name is hashed,
  // so the card is found structurally: climb while the parent is roughly the
  // same size, and stop when it balloons into the whole results list.
  const cardOf = a => {
    const li = a.closest('li');
    if (li) return li;
    let n = a.parentElement, best = n;
    for (let i = 0; i < 8 && n && n.parentElement; i++) {
      const cur = clean(n.textContent).length;
      const par = clean(n.parentElement.textContent).length;
      if (cur > 40 && par > cur * 3) break;
      n = n.parentElement; best = n;
    }
    return best;
  };

  // A card links to its subject twice (photo + name) and often to other people
  // too. One card yields exactly one person: the first profile linked in it.
  const bySlug = new Map();
  const claimed = new Set();
  for (const a of scope.querySelectorAll('a[href*="/in/"]')) {
    const m = (a.getAttribute('href') || '').match(/\/in\/([^/?#]+)/);
    if (!m) continue;
    if (inMutualBlock(a)) continue;
    const slug = decodeURIComponent(m[1]);
    const t = textOf(a);

    const prev = bySlug.get(slug);
    if (prev) {
      if (!prev.text && good(t)) prev.text = t;
      continue;
    }
    const card = cardOf(a);
    if (!card || claimed.has(card)) continue;
    claimed.add(card);
    bySlug.set(slug, { anchor: a, card, text: good(t) ? t : '' });
  }

  const out = [];
  for (const [slug, hit] of bySlug) {
    const card = hit.card;

    // Ordered, de-duplicated text from leaf elements only.
    const lines = [];
    const push = t => { t = clean(t); if (t && !lines.includes(t)) lines.push(t); };
    for (const el of card.querySelectorAll('span, div, p, h3, h4')) {
      if (el.querySelector('span, div, p, h3, h4')) continue;
      push(textOf(el));
    }
    if (!lines.length) push(textOf(card));

    const name = hit.text || lines.find(good) || '';
    if (!name) continue;

    const degLine = lines.find(t => /\b[123](?:st|nd|rd)\b/.test(t)) || '';
    const degree = (degLine.match(/\b([123](?:st|nd|rd))\b/) || [, ''])[1];

    const headline = lines.find(t => t !== name && !NOISE.test(t) && t.length > 2) || '';

    out.push({
      slug, name, headline, degree,
      profile_url: 'https://www.linkedin.com/in/' + slug + '/',
      raw_lines: lines.slice(0, 8).join(' | '),
    });
  }
  return out;
}
"""

SCROLL_JS = r"""
() => {
  const dialogs = [...document.querySelectorAll('[role="dialog"]')]
    .filter(d => d.offsetParent !== null && d.querySelector('a[href*="/in/"]'));
  const root = dialogs.length ? dialogs[dialogs.length - 1] : null;

  if (root) {
    const cands = [root, ...root.querySelectorAll('*')].filter(el => {
      const s = getComputedStyle(el);
      return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 50;
    });
    const sc = cands.sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
    if (sc) { sc.scrollTop = sc.scrollHeight; return { kind: 'dialog', height: sc.scrollHeight }; }
  }
  const se = document.scrollingElement || document.documentElement;
  se.scrollTop = se.scrollHeight;
  return { kind: 'window', height: se.scrollHeight };
}
"""

LOAD_MORE = ["Show more results", "Show more", "Load more", "See more"]


async def _click_load_more(page) -> bool:
    for label in LOAD_MORE:
        try:
            btn = page.get_by_role("button", name=re.compile(label, re.I)).first
            if await btn.is_visible(timeout=250):
                await btn.click(timeout=1500)
                return True
        except Exception:
            continue
    return False


def _split_headline(headline: str) -> tuple[str, str]:
    """'VP Marketing at Acme' -> ('VP Marketing', 'Acme'). Raw value is kept
    alongside, so a bad split is visible rather than lossy."""
    if not headline:
        return "", ""
    # " @Company" (no trailing space) is as common as " at Company".
    parts = re.split(r"\s+(?:at|@)\s*", headline, maxsplit=1, flags=re.I)
    if len(parts) == 2:
        # Headlines pile several claims behind separators - keep only the
        # first fragment as the company name.
        company = re.split(r"\s*[|·•‧/]\s*", parts[1])[0].strip()
        return parts[0].strip(), company
    return headline.strip(), ""


FIELDS = ["name", "title", "company", "headline", "degree", "profile_url",
          "slug", "event_name", "event_url", "scraped_at", "raw_lines"]


async def harvest(page, on_progress: Optional[Callable[[dict], None]] = None,
                  event_name: str = "", event_url: str = "") -> list[dict]:
    seen: dict[str, dict] = {}
    stagnant = 0
    started = time.monotonic()
    scraped_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def report(**kw):
        if on_progress:
            on_progress(kw)

    for round_no in range(1, config.MAX_ROUNDS + 1):
        try:
            rows = await page.evaluate(EXTRACT_JS)
        except Exception as exc:
            report(message=f"Extraction failed: {exc}")
            break

        new = 0
        for r in rows:
            slug = r.get("slug")
            if not slug or slug in seen:
                continue
            title, company = _split_headline(r.get("headline", ""))
            seen[slug] = {
                "name": r.get("name", ""),
                "title": title,
                "company": company,
                "headline": r.get("headline", ""),
                "degree": r.get("degree", ""),
                "profile_url": r.get("profile_url", ""),
                "slug": slug,
                "event_name": event_name,
                "event_url": event_url,
                "scraped_at": scraped_at,
                "raw_lines": r.get("raw_lines", ""),
            }
            new += 1

        report(count=len(seen), rounds=round_no,
               message=f"Round {round_no}: {len(seen)} attendees found")

        if len(seen) >= config.MAX_ATTENDEES:
            report(message=f"Hit MAX_ATTENDEES ({config.MAX_ATTENDEES}); stopping.")
            break
        if time.monotonic() - started > config.MAX_SECONDS:
            report(message=f"Hit MAX_SECONDS ({config.MAX_SECONDS}); stopping.")
            break

        clicked = await _click_load_more(page)
        try:
            await page.evaluate(SCROLL_JS)
        except Exception:
            pass
        await page.wait_for_timeout(config.SETTLE_MS)

        if new == 0 and not clicked:
            stagnant += 1
            if stagnant >= config.STAGNANT_LIMIT:
                report(message=f"No new attendees in {stagnant} rounds; finished.")
                break
        else:
            stagnant = 0

    return list(seen.values())
