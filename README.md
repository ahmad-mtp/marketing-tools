# marketing-tools

Tools and automation workflows for the marketing department.

Each tool lives in its own directory with its own dependencies, configuration
and documentation. There is no shared runtime — a tool can be copied to
someone's machine on its own and still work.

| Tool | What it does |
|---|---|
| [`event-enrichment/`](event-enrichment/) | Turns a LinkedIn event's attendee list into a CSV of names, titles, companies, emails and phone numbers, via Apollo |

## Adding a tool

Create a directory, keep everything it needs inside it, and give it a README a
colleague could follow without asking anyone questions. Add a row above.

## Handling data

Several of these tools produce files containing real people's contact details.
The repository `.gitignore` excludes the obvious ones — `data/`, `.env`,
`*_event/`, `attendees*.csv` — but the responsibility is yours before you
commit. Check `git status` if you are unsure.
