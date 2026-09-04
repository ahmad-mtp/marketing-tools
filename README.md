# marketing-tools

Tools and automation workflows for our marketing department.

## LinkedIn Event Attendee Harvester

Turns a LinkedIn event's **Attendees** list into a CSV. The operative signs in
themselves — the tool never sees or stores credentials.

### The flow

1. **Launch session** — Chrome opens on a profile the tool owns.
2. Operative signs in to LinkedIn (2FA on first run only; the session persists).
3. Paste the event's attendee-list URL and press **Open event** — the tool
   drives the tab itself rather than guessing which tab you meant.
4. **Scrape attendees** — auto-scrolls the list and harvests every profile.
5. **Download CSV** — lands in the operative's normal Downloads folder.

The dashboard polls the session and only enables *Scrape* once it can see a
signed-in LinkedIn session and an open event tab, so step 3 can't be pressed
too early.

---

### Mode A — dedicated profile (recommended)

Playwright launches Chrome on a profile the tool owns. No Docker.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh                 # or ./run.sh acme-corp for a per-client profile
```

Open <http://127.0.0.1:8000>. Chrome profiles live in `data/profiles/<client>/`,
so sign-in and 2FA are only needed the first time per client.

#### Why not your everyday Chrome profile?

Chrome refuses remote debugging on its default profile directory —
*"DevTools remote debugging requires a non-default data directory"* — a
deliberate protection against anything attaching to your real browser. So
`--real` is impossible and the launcher rejects it with that explanation.

`scripts/launch-chrome.sh --clone` copies the session files into a directory
Chrome will accept. **This did not carry the LinkedIn session over on macOS**
in our testing, most likely because Chrome binds cookie encryption to a
Keychain key. It may work on Linux, where cookie encryption differs. Treat it
as unsupported and sign in once on a dedicated profile instead — that is a
one-time cost per client.

### Mode B — Docker Compose

The container runs **Python only**. Chrome stays on the host, because the
operative needs a real window to sign in — so the browser is started by a host
script and the backend attaches to it over CDP.

```bash
./scripts/launch-chrome.sh acme-corp     # host: Chrome on port 9222
CLIENT=acme-corp docker compose up --build
```

Open <http://127.0.0.1:8000> and press **Connect to Chrome**.

On Windows use `scripts\launch-chrome.bat` instead.

> **Security:** the launcher passes `--remote-debugging-address=0.0.0.0`, which
> exposes full control of that signed-in browser to anything that can reach
> port 9222. Trusted networks only, and quit Chrome when finished. Before wider
> rollout this should be bound to the Docker bridge address specifically.

---

### CSV columns

| column | source |
|---|---|
| `name` | attendee card |
| `title`, `company` | split from `headline` on " at " / " @ " |
| `headline` | raw, kept so a bad split stays visible |
| `degree` | 1st / 2nd / 3rd |
| `profile_url`, `slug` | the profile link |
| `event_name`, `event_url`, `scraped_at` | run metadata |
| `raw_lines` | first 8 text lines of the card — for tuning selectors |

### Configuration

All env-overridable (see `app/config.py`):

| var | default | meaning |
|---|---|---|
| `BROWSER_MODE` | `launch` | `launch` or `cdp` |
| `CLIENT` | `default` | Chrome profile name |
| `CDP_HOST` / `CDP_PORT` | `host.docker.internal` / `9222` | host Chrome endpoint |
| `MAX_ROUNDS` | `400` | scroll rounds before stopping |
| `MAX_SECONDS` | `600` | wall-clock cap |
| `MAX_ATTENDEES` | `5000` | hard row cap |
| `SETTLE_MS` | `900` | pause after each scroll |
| `STAGNANT_LIMIT` | `3` | rounds with no new rows before finishing |

### Tests

```bash
.venv/bin/python tests/test_harvest.py
```

Runs the full harvest against `tests/fixture_event.html`, a local page that
reproduces LinkedIn's awkward bits — duplicated screen-reader text, separate
photo and name links to the same profile, and a lazy-loading scroll container.
Verifies all 60 fixture attendees are captured, deduped and exported.

### When selectors break

LinkedIn reshuffles its DOM periodically. Extraction is anchored on
`a[href*="/in/"]`, which is the most stable signal on the page, but name and
headline are heuristic.

Press **Dump DOM** in the dashboard to save the live page to
`data/debug/event-*.html`, then tune `EXTRACT_JS` in `app/scraper.py` against
it. The `raw_lines` CSV column shows what the extractor actually saw for each
card, which is usually enough to spot the problem without a dump.

### Scope

List harvest only. It does not visit individual profiles and does no
third-party enrichment — that keeps the LinkedIn account well clear of the
bulk-profile-view patterns that trigger rate limiting.





