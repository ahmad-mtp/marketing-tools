# Event Enrichment

Turns a LinkedIn event's attendee list into a CSV of names, titles, companies,
**emails and phone numbers**.

Two phases, both driven from one local dashboard:

1. **Harvest** — a Chrome window under automation scrolls the attendee list and
   collects every profile.
2. **Enrich** — those profiles go to Apollo's bulk endpoint, which returns
   contact details. This never touches LinkedIn, so it takes minutes rather
   than the hours a browser walk of the same list would.

Output is a single CSV. Nothing is uploaded anywhere except Apollo.

---

## Requirements

- **macOS or Linux** (Windows: the launcher has a `.bat`, the rest is untested)
- **Python 3.12+**
- **Google Chrome**
- **An Apollo API key** with credits — *and mobile number credits if you want
  phone numbers, which are billed separately*
- **A publicly reachable URL**, for phone numbers only (see below)

---

## One-time setup

```bash
cd event-enrichment
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then add your Apollo key
```

### Clone your Chrome profile

Chrome **refuses remote debugging on its default profile directory** — a
deliberate protection against anything attaching to your real browser. So the
tool works on a *copy* of your profile, which Chrome does allow.

```bash
# Quit Chrome completely first: Cmd+Q, not just closing the window.
./scripts/launch-chrome.sh --clone
```

This copies your cookies, logins **and extensions** into
`~/.linkedin-harvester/profiles/default`, then launches Chrome on the copy with
a debugging port. You stay signed in to LinkedIn — no second login, no 2FA.

Your real profile is only read, never modified. If the clone breaks, delete the
directory and clone again.

> **Quit with Cmd+Q, never `kill`.** A force-kill does not let Chrome flush its
> cookie store, and the LinkedIn session cookie is lost. This has bitten us.

Later runs don't need `--clone` — plain `./scripts/launch-chrome.sh` reuses the
existing copy and coexists with your everyday Chrome.

---

## Running it

```bash
./scripts/launch-chrome.sh      # terminal 1 — the browser being driven
./run.sh --attach               # terminal 2 — the dashboard
```

Open **http://127.0.0.1:8000**, then:

1. **Connect to Chrome** → shows `Connected ✓`
2. Paste the event's attendee-list URL → **Open event**
3. **Scrape attendees** — watch the counter climb
4. **Enrich with Apollo** — set a **Limit** first for a small test
5. **Save CSV to disk** — writes to `~/Downloads` and shows the path

The blue highlight always sits on the next thing to press.

> Use **Save CSV to disk**, not **Download CSV**, when viewing the dashboard
> inside the automated Chrome. A CDP-controlled browser silently discards
> ordinary downloads. Opening the dashboard in your normal browser avoids this.

The browser can never run inside a container — it needs a display and a human.
`docker-compose.yml` runs the backend only, attaching to Chrome on the host.

---

## Phone numbers

Apollo does **not** return phone numbers in the enrichment response. It returns
demographics immediately, delivers phones asynchronously, and **refuses the
request entirely unless you supply a `webhook_url`**.

The tool reads phones back by polling `/webhook_result/{request_id}`, so that
URL does not have to receive anything — but it must exist and respond.

```bash
# .env
APOLLO_REVEAL_PHONE=true
APOLLO_WEBHOOK_URL=https://your-public-url/api/apollo/webhook
```

`app/webhook_receiver.py` is a ~40-line app serving exactly that one endpoint:

```bash
.venv/bin/python -m uvicorn app.webhook_receiver:app --port 8787
ssh -R 80:localhost:8787 nokey@localhost.run     # prints a public URL
```

**Deploy the receiver properly before relying on this.** Free tunnels get a new
random URL each start and die after a few hours — three died in one afternoon
during development. The receiver is small enough for a Cloudflare Worker or a
Vercel function, which gives a permanent URL and removes the problem.

The receiver is deliberately a **separate app** from the dashboard. Exposing the
dashboard would put `/api/scrape` and `/api/connect` on the public internet,
where anyone with the URL could drive your signed-in browser.

Before spending credits the dashboard checks the webhook URL is alive and
refuses to start if it is not.

---

## Output

`name, company, title, email, phone` by default. Apollo's values win where
present — its titles are materially cleaner than LinkedIn headlines.

Add `?full=1` to `/api/export.csv` or `/api/export-file` for all 28 columns,
including `degree`, `profile_url`, `apollo_seniority`, `apollo_industry`,
`apollo_company_size`, `apollo_email_status` and `apollo_phone_confidence`.

**`apollo_status` explains every blank.** `matched` with empty contact fields
means Apollo knows the person but holds no email or phone; `no_match` means it
could not identify them. A blank is never ambiguous.

**Check `apollo_email_status` before sending.** `verified` is solid;
`extrapolated` is Apollo's guess from a company's address pattern and will
bounce more often. The tool never constructs an email address itself.

Rows persist in `data/last_run.json`, so a restart mid-run loses nothing.

---

## Configuration

All in `.env`, all optional except the key.

| Variable | Default | Meaning |
|---|---|---|
| `APOLLO_API_KEY` | — | **Required** |
| `APOLLO_REVEAL_PERSONAL_EMAILS` | `true` | Extra credits per match |
| `APOLLO_REVEAL_PHONE` | `true` | Requires `APOLLO_WEBHOOK_URL` |
| `APOLLO_WEBHOOK_URL` | — | Mandatory when revealing phones |
| `APOLLO_BATCH_SIZE` | `10` | Apollo's hard cap |
| `APOLLO_BATCH_DELAY_MS` | `1000` | Pause between batches |
| `APOLLO_PHONE_POLL_TIMEOUT_S` | `600` | How long to wait for phones |
| `BROWSER_MODE` | `launch` | `launch`, or `cdp` (what `--attach` sets) |
| `CLIENT` | `default` | Chrome profile name, one per client |
| `PROFILE_DELAY_MIN_MS` / `MAX_MS` | `120000` | Dwell per profile in phase 2 |
| `EXPORT_DIR` | `~/Downloads` | Where **Save CSV to disk** writes |

---

## Phase 2: opening profiles

Optional, and separate from Apollo. It walks the scraped profiles in the live
browser, holding each open for a configurable dwell so anything running on the
page — an Apollo or similar extension — has time to work.

**Bulk profile views are the pattern LinkedIn rate-limits.** At the default
two-minute dwell, 263 profiles is roughly nine hours of continuous automation
on a signed-in account. Apollo's API does the same job in minutes without
touching LinkedIn. Use phase 2 only when you specifically need the page visited.

Start with a **Limit** of 10–25 and watch for captchas or blank profiles.

---

## Troubleshooting

**"DevTools remote debugging requires a non-default data directory"**
Chrome will not debug your real profile. Use `--clone`. `--real` is impossible
and the launcher refuses it with this explanation.

**`--clone` says Chrome is running**
Quit it with Cmd+Q. Copying a live profile yields a corrupt cookie database.

**"Browser context management is not supported"**
The browser has no open window — Chrome stays alive on macOS with every window
closed. The tool now opens a blank tab automatically; if you still see it, the
Chrome on `:9222` has died. Relaunch it.

**Scrape returns 0 rows**
Check the tab the tool drove. If LinkedIn redirected to `/uas/login`, that
profile is signed out. Sign in once in that window; it persists in the clone.

**Names or titles look wrong**
Press **Dump DOM** to save the live page to `data/debug/`, then check the
`raw_lines` column in a full export — it shows what the extractor saw for each
card. Extraction anchors on `a[href*="/in/"]`, which does not move; the rest is
heuristic and LinkedIn reshuffles its DOM periodically.

**Phones come back as 0**
Look for the warning in the dashboard. Apollo reports reasons such as
*"you ran out of mobile number credits"* — a billing state, not a bug.
Everything is logged to `data/logs/apollo.log` and `webhook.log`.

**Chrome updated itself mid-session**
It closes the automated window. Relaunch and press Connect. Worth knowing
before a live demo, because it looks exactly like a tool failure.

---

## Tests

```bash
.venv/bin/python tests/test_harvest.py        # attendee-modal DOM shape
.venv/bin/python tests/test_search_shape.py   # people-search DOM shape
.venv/bin/python tests/test_profiles.py       # phase 2 pacing, limits, stop
.venv/bin/python tests/test_apollo.py         # Apollo client, mocked
```

No test touches LinkedIn or spends an Apollo credit.

The fixtures reproduce the real thing: search results are **not** in `<li>`,
every class name is hashed, each result is linked six times, and every card
ends with an *"X & 2 other mutual connections"* block linking to people who are
**not** on the list. Scraping those polluted an early CSV; the search fixture
plants 80 profile links across 10 results and asserts exactly 10 people come
back.

---

## Limitations

- **A session the tool cannot attach to.** An incognito window, or a login you
  cannot re-create, cannot be cloned or driven. The `bulk-snippet-workaround`
  branch handles that case with a console snippet pasted into the operator's
  own browser.
- **Free tunnels are not fit for routine use.** Deploy the receiver.
- **One operator per running instance.** State is a module-level singleton.
- **LinkedIn's terms prohibit automated scraping.** Enforcement in practice is
  rate limiting, then restriction. Reading one page you already opened is a
  very different risk from bulk profile visits.
- **Contact data is personal data.** For EU or UK attendees you need a lawful
  basis to process it, and to honour deletion requests. That call belongs to
  whoever owns the campaign.
