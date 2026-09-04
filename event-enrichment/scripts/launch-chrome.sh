#!/usr/bin/env bash
# Start Chrome with a debugging port the harvester can attach to.
#
#   ./scripts/launch-chrome.sh --clone          # full copy of your everyday
#                                               #   profile: cookies, logins AND
#                                               #   extensions. Stay signed in.
#   ./scripts/launch-chrome.sh --clone --force  # re-copy over an existing clone
#   ./scripts/launch-chrome.sh --sync-cookies   # refresh just the session into
#                                               #   an existing clone (fast)
#   PROFILE_PATH=/path/to/profile ./scripts/launch-chrome.sh
#                                               # use a profile directory you
#                                               #   already keep for automation
#   ./scripts/launch-chrome.sh acme-corp        # dedicated profile, sign in once
#
# WHY A COPY: Chrome refuses remote debugging on its DEFAULT profile directory
# ("DevTools remote debugging requires a non-default data directory") to stop
# anything attaching to your real browser. Any other directory is accepted, so
# a copy at a different path works and keeps you signed in.
#
# The debug port listens on 127.0.0.1 only unless --docker is passed.
set -euo pipefail

CLIENT="default"; USE_CLONE=0; SYNC_COOKIES=0; FORCE=0; USE_REAL=0; FOR_DOCKER=0
for arg in "$@"; do
  case "$arg" in
    --clone)         USE_CLONE=1 ;;
    --sync-cookies)  SYNC_COOKIES=1 ;;
    --force)         FORCE=1 ;;
    --real)          USE_REAL=1 ;;
    --docker)        FOR_DOCKER=1 ;;
    -*)              echo "Unknown option: $arg" >&2; exit 2 ;;
    *)               CLIENT="$arg" ;;
  esac
done

PORT="${CDP_PORT:-9222}"
PROFILE_DIRECTORY="${PROFILE_DIRECTORY:-Default}"

case "$(uname -s)" in
  Darwin)
    CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
    REAL_PROFILE="$HOME/Library/Application Support/Google/Chrome" ;;
  *)
    CHROME="${CHROME:-$(command -v google-chrome || command -v google-chrome-stable || command -v chromium)}"
    REAL_PROFILE="$HOME/.config/google-chrome" ;;
esac

[ -x "$CHROME" ] || { echo "Chrome not found. Set CHROME=/path/to/chrome." >&2; exit 1; }

if [ "$USE_REAL" = 1 ]; then
  echo "--real cannot work: Chrome blocks remote debugging on its default profile" >&2
  echo "directory, so it would start with no debug port. Use --clone instead." >&2
  exit 2
fi

PROFILE="${PROFILE_PATH:-${HOME}/.linkedin-harvester/profiles/${CLIENT}}"

# Chrome must be closed: a live profile has locked, half-written databases.
chrome_running() { pgrep -f "Google Chrome" >/dev/null 2>&1 || pgrep -x chrome >/dev/null 2>&1; }
require_chrome_closed() {
  if chrome_running; then
    echo "Chrome is running. Quit it completely (Cmd+Q on macOS - closing the" >&2
    echo "window is not enough), then run this again. Copying a live profile" >&2
    echo "gives a corrupt cookie database." >&2
    exit 1
  fi
}

# Everything that is pure cache, lock state or crash telemetry.
RSYNC_EXCLUDES=(
  --exclude "Default/Cache/"            --exclude "Default/Code Cache/"
  --exclude "Default/GPUCache/"         --exclude "Default/DawnCache/"
  --exclude "Default/Service Worker/CacheStorage/"
  --exclude "Default/Service Worker/ScriptCache/"
  --exclude "ShaderCache/"              --exclude "GrShaderCache/"
  --exclude "GraphiteDawnCache/"        --exclude "Crashpad/"
  --exclude "BrowserMetrics/"           --exclude "component_crx_cache/"
  --exclude "extensions_crx_cache/"     --exclude "Safe Browsing/"
  --exclude "optimization_guide_model_store/"
  --exclude "Singleton*"
)

SESSION_FILES=("Default/Cookies" "Default/Login Data" "Default/Preferences"
               "Default/Web Data" "Default/Network/Cookies" "Local State")

copy_session_files() {
  for f in "${SESSION_FILES[@]}"; do
    if [ -f "$REAL_PROFILE/$f" ]; then
      mkdir -p "$PROFILE/$(dirname "$f")"
      cp -f "$REAL_PROFILE/$f" "$PROFILE/$f"
    fi
  done
}

if [ "$SYNC_COOKIES" = 1 ]; then
  require_chrome_closed
  [ -d "$PROFILE" ] || { echo "No clone at $PROFILE - run --clone first." >&2; exit 1; }
  copy_session_files
  echo "Refreshed session files from your everyday profile."
elif [ "$USE_CLONE" = 1 ]; then
  require_chrome_closed
  [ -d "$REAL_PROFILE" ] || { echo "No Chrome profile at: $REAL_PROFILE" >&2; exit 1; }
  if [ -d "$PROFILE/Default" ] && [ "$FORCE" = 0 ]; then
    echo "Clone already exists at $PROFILE"
    echo "Refreshing session files only (use --force for a full re-copy)."
    copy_session_files
  else
    echo "Cloning your Chrome profile (cookies, logins and extensions)."
    echo "  from: $REAL_PROFILE"
    echo "  to:   $PROFILE"
    mkdir -p "$PROFILE"
    rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$REAL_PROFILE/" "$PROFILE/"
    echo "Cloned: $(du -sh "$PROFILE" | cut -f1), $(ls "$PROFILE/Default/Extensions" 2>/dev/null | wc -l | tr -d ' ') extensions."
  fi
else
  mkdir -p "$PROFILE"
fi

ADDR_ARGS=(--remote-debugging-address=127.0.0.1)
if [ "$FOR_DOCKER" = 1 ]; then
  ADDR_ARGS=(--remote-debugging-address=0.0.0.0)
  echo "WARNING: debug port open on 0.0.0.0 - anyone who can reach port ${PORT}"
  echo "         gets full control of this signed-in browser. Trusted networks only."
fi

echo "Profile: $PROFILE"
echo "CDP:     http://127.0.0.1:${PORT}"
echo

exec "$CHROME" \
  --remote-debugging-port="${PORT}" \
  "${ADDR_ARGS[@]}" \
  --remote-allow-origins='*' \
  --user-data-dir="$PROFILE" \
  --profile-directory="$PROFILE_DIRECTORY" \
  --no-first-run \
  --no-default-browser-check \
  "https://www.linkedin.com/"
