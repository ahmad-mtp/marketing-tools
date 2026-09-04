#!/usr/bin/env bash
# Start the harvester dashboard.
#
#   ./run.sh                 # Playwright launches its own Chrome (fresh login)
#   ./run.sh acme-corp       # ...on a per-client profile
#   ./run.sh --attach        # attach to a Chrome you started yourself with
#                            #   scripts/launch-chrome.sh --real  (already signed in)
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "--attach" ]; then
  export BROWSER_MODE=cdp
  export CDP_HOST="${CDP_HOST:-127.0.0.1}"
  export CLIENT="${2:-default}"
  echo "Attach mode - expecting Chrome on ${CDP_HOST}:${CDP_PORT:-9222}"
else
  export BROWSER_MODE=launch
  export CLIENT="${1:-default}"
fi

echo "Dashboard: http://127.0.0.1:8000   (mode: $BROWSER_MODE, client: $CLIENT)"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
