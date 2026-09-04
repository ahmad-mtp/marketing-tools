"""Owns the single Playwright session shared across HTTP requests.

Two modes, one interface:
  launch - Playwright starts Chrome on a profile it owns (fresh login needed).
  cdp    - attach to a Chrome the operative started themselves, using their
           real everyday profile. Already signed in, so no second login and
           no 2FA. Started by scripts/launch-chrome.sh.
"""
import asyncio
import json
import re
import socket
import urllib.request
from typing import Any, Optional

from playwright.async_api import async_playwright

from . import config

EVENT_URL_RE = re.compile(r"linkedin\.com/events/", re.I)
LINKEDIN_RE = re.compile(r"://[^/]*linkedin\.com/", re.I)
BLOCKED_URL_RE = re.compile(r"linkedin\.com/(login|checkpoint|uas/)", re.I)
# The dashboard itself is served from localhost and is often open in the very
# browser we drive. Navigating it away destroys the operator's UI.
DASHBOARD_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?(/|$)", re.I)


def _cdp_endpoint() -> str:
    """Chrome's DevTools endpoint rejects requests whose Host header is not
    localhost or a bare IP, so `host.docker.internal:9222` is refused from a
    container. Resolving to an IP first makes the Host header an IP, which
    Chrome accepts. Harmless when the host is already 127.0.0.1."""
    try:
        ip = socket.gethostbyname(config.CDP_HOST)
    except socket.gaierror:
        ip = config.CDP_HOST
    return f"http://{ip}:{config.CDP_PORT}"


class BrowserSession:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None            # only set in cdp mode
        self._context = None
        self._active_page = None        # the tab we drove to the event
        self.lock = asyncio.Lock()
        self.last_error: Optional[str] = None

    # ---- lifecycle -------------------------------------------------
    async def startup(self) -> None:
        self._pw = await async_playwright().start()

    async def shutdown(self) -> None:
        await self.disconnect()
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def connect(self) -> dict:
        async with self.lock:
            if self._context is not None and self._alive():
                return await self.status()
            self.last_error = None
            self._active_page = None
            try:
                if config.BROWSER_MODE == "cdp":
                    await self._connect_cdp()
                else:
                    await self._launch()
            except Exception as exc:                    # surfaced in the UI
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._context = None
                self._browser = None
            return await self.status()

    @staticmethod
    def _ensure_page_target(endpoint: str) -> None:
        """Playwright's connect_over_cdp fails with a misleading
        'Browser context management is not supported' when the browser has no
        page targets at all - which happens whenever every window is closed
        but Chrome keeps running. Open a blank tab first."""
        try:
            with urllib.request.urlopen(f"{endpoint}/json/list", timeout=5) as r:
                targets = json.loads(r.read().decode())
        except Exception:
            return
        if any(t.get("type") == "page" for t in targets):
            return
        try:
            req = urllib.request.Request(f"{endpoint}/json/new?url=about:blank", method="PUT")
            urllib.request.urlopen(req, timeout=8).read()
        except Exception:
            pass

    async def _connect_cdp(self) -> None:
        endpoint = _cdp_endpoint()
        await asyncio.to_thread(self._ensure_page_target, endpoint)
        self._browser = await self._pw.chromium.connect_over_cdp(endpoint, timeout=10_000)
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError(
                "Chrome is reachable but exposes no browsing context. "
                "Make sure it was started by scripts/launch-chrome.sh and has a tab open.")
        # Prefer the context that already has a LinkedIn tab.
        self._context = next(
            (c for c in contexts
             if any(LINKEDIN_RE.search(p.url or "") for p in c.pages if not p.is_closed())),
            contexts[0])

    async def _launch(self) -> None:
        profile = config.PROFILE_DIR / config.CLIENT
        profile.mkdir(parents=True, exist_ok=True)
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",          # drive real Chrome, no browser download
            headless=False,
            viewport=None,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )
        page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._active_page = page
        try:
            await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass                        # operative can navigate manually

    async def disconnect(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()      # cdp: detaches, leaves Chrome up
            elif self._context is not None:
                await self._context.close()      # launch: closes the window we opened
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._active_page = None

    # ---- introspection ---------------------------------------------
    def _alive(self) -> bool:
        if self._context is None:
            return False
        try:
            _ = self._context.pages
            return True
        except Exception:
            return False

    @property
    def context(self):
        """The context new tabs should be created in."""
        if not self._alive():
            raise RuntimeError("Not connected to Chrome.")
        return self._context

    def pages(self) -> list:
        """Every open tab across every context, so a tab opened in another
        window is still visible to us."""
        if not self._alive():
            return []
        contexts = self._browser.contexts if self._browser is not None else [self._context]
        out = []
        for ctx in contexts:
            try:
                out.extend(p for p in ctx.pages if not p.is_closed())
            except Exception:
                continue
        return out

    async def goto(self, url: str):
        """Drive a tab straight to the event/attendees URL. Removes the guesswork
        of asking the operative to navigate and us guessing which tab they meant."""
        if not self._alive():
            raise RuntimeError("Not connected to Chrome.")
        page = self._active_page
        if page is None or page.is_closed() or DASHBOARD_RE.search(page.url or ""):
            # Reuse a LinkedIn tab if there is one; otherwise open a new tab.
            # Never reuse the dashboard tab or any unrelated page.
            page = next((p for p in self.pages()
                         if LINKEDIN_RE.search(p.url or "")
                         and not DASHBOARD_RE.search(p.url or "")), None)
        if page is None or page.is_closed():
            page = await self._context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(1200)
        self._active_page = page
        try:
            await page.bring_to_front()
        except Exception:
            pass
        return page

    async def event_page(self):
        """The tab we drove, if it's still alive; otherwise any LinkedIn event tab."""
        if self._active_page is not None and not self._active_page.is_closed():
            url = self._active_page.url or ""
            if LINKEDIN_RE.search(url) and not DASHBOARD_RE.search(url):
                return self._active_page
        for page in self.pages():
            if EVENT_URL_RE.search(page.url or ""):
                return page
        return None

    async def _logged_in(self) -> bool:
        """Check every context - with CDP the LinkedIn cookies may live in a
        different context than the one we picked."""
        contexts = self._browser.contexts if self._browser is not None else [self._context]
        for ctx in contexts:
            try:
                cookies = await ctx.cookies("https://www.linkedin.com")
            except Exception:
                continue
            if any(c.get("name") == "li_at" and c.get("value") for c in cookies):
                return True
        return False

    async def status(self) -> dict:
        mode = config.BROWSER_MODE
        base: dict[str, Any] = {
            "mode": mode,
            "client": config.CLIENT,
            "connected": False,
            "logged_in": False,
            "event_url": None,
            "event_name": None,
            "tabs": [],
            "tab_count": 0,
            "ready": False,
            "stage": "disconnected",
            "hint": ("Start Chrome with scripts/launch-chrome.sh, then press Connect."
                     if mode == "cdp" else "Press Launch session to open Chrome."),
            "error": self.last_error,
        }
        if not self._alive():
            return base

        pages = self.pages()
        base["connected"] = True
        base["tab_count"] = len(pages)
        base["tabs"] = [(p.url or "")[:140] for p in pages]
        # Reported for information only. It is never a gate: the operator is
        # assumed to already have a usable window open in this profile, and a
        # signed-out session simply scrapes nothing, which is self-evident.
        base["logged_in"] = await self._logged_in()

        page = await self.event_page()
        if page is None:
            base["stage"] = "awaiting_event"
            base["hint"] = "Connected. Paste the event or attendees URL below and press Open event."
            return base

        base["event_url"] = page.url
        try:
            title = await page.title()
            base["event_name"] = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title).strip() or None
        except Exception:
            pass
        base["stage"] = "ready"
        base["ready"] = True
        if BLOCKED_URL_RE.search(page.url or ""):
            base["hint"] = ("LinkedIn is showing a login or checkpoint page - scraping it will "
                            "return nothing. Clear it in Chrome, then reload the event URL.")
        else:
            base["hint"] = "Ready. Make sure the attendee list is on screen, then press Scrape attendees."
        return base


session = BrowserSession()
