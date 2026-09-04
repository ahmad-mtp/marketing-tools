"""Runtime configuration. Everything is env-overridable so the same image
runs bare on a laptop (BROWSER_MODE=launch) or in compose (BROWSER_MODE=cdp)."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
PROFILE_DIR = DATA_DIR / "profiles"
DEBUG_DIR = DATA_DIR / "debug"

# launch = Playwright opens Chrome itself (bare metal, host has a display)
# cdp    = attach to a Chrome already running on the host (container-friendly)
BROWSER_MODE = os.getenv("BROWSER_MODE", "launch").strip().lower()

CDP_HOST = os.getenv("CDP_HOST", "host.docker.internal")
CDP_PORT = int(os.getenv("CDP_PORT", "9222"))

# One Chrome profile per client keeps their LinkedIn sessions separate.
CLIENT = os.getenv("CLIENT", "default")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# Safety caps so a scroll loop can never run away.
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "400"))
MAX_SECONDS = int(os.getenv("MAX_SECONDS", "600"))
MAX_ATTENDEES = int(os.getenv("MAX_ATTENDEES", "5000"))
SETTLE_MS = int(os.getenv("SETTLE_MS", "900"))
STAGNANT_LIMIT = int(os.getenv("STAGNANT_LIMIT", "3"))

for _d in (DATA_DIR, PROFILE_DIR, DEBUG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- phase 2: opening profiles -------------------------------------------
# Bulk profile views are the pattern LinkedIn rate-limits, so the defaults
# are deliberately unhurried and capped.
PROFILE_DELAY_MIN_MS = int(os.getenv("PROFILE_DELAY_MIN_MS", "120000"))
PROFILE_DELAY_MAX_MS = int(os.getenv("PROFILE_DELAY_MAX_MS", "120000"))
PROFILE_MAX_TABS = int(os.getenv("PROFILE_MAX_TABS", "20"))
PROFILE_MAX_VISITS = int(os.getenv("PROFILE_MAX_VISITS", "500"))
LAST_RUN_FILE = DATA_DIR / "last_run.json"


# --- phase 3: Apollo enrichment ------------------------------------------
APOLLO_API_KEY = os.getenv("APOLLO_API_KEY", "").strip()
APOLLO_BASE_URL = os.getenv("APOLLO_BASE_URL", "https://api.apollo.io").rstrip("/")
# Apollo caps bulk enrichment at 10 records per request.
APOLLO_BATCH_SIZE = max(1, min(10, int(os.getenv("APOLLO_BATCH_SIZE", "10"))))
APOLLO_BATCH_DELAY_MS = int(os.getenv("APOLLO_BATCH_DELAY_MS", "1000"))
APOLLO_TIMEOUT_S = float(os.getenv("APOLLO_TIMEOUT_S", "45"))
APOLLO_MAX_RETRIES = int(os.getenv("APOLLO_MAX_RETRIES", "4"))


def _flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# Both contact channels are always wanted, so both default on.
APOLLO_REVEAL_PERSONAL_EMAILS = _flag("APOLLO_REVEAL_PERSONAL_EMAILS", "true")
APOLLO_REVEAL_PHONE = _flag("APOLLO_REVEAL_PHONE", "true")

# Apollo makes webhook_url mandatory whenever reveal_phone_number is true, even
# though we read the phones back by polling /webhook_result/{request_id}
# instead of waiting to be called. Point it at this app's receiver if you have
# a tunnel; otherwise any URL you control satisfies the requirement.
APOLLO_WEBHOOK_URL = os.getenv("APOLLO_WEBHOOK_URL", "").strip()
# Phone enrichment is asynchronous and Apollo warns it can take several minutes.
APOLLO_PHONE_POLL_INTERVAL_S = float(os.getenv("APOLLO_PHONE_POLL_INTERVAL_S", "10"))
APOLLO_PHONE_POLL_TIMEOUT_S = float(os.getenv("APOLLO_PHONE_POLL_TIMEOUT_S", "600"))


# Where "Save CSV to disk" writes. A CDP-controlled Chrome silently discards
# ordinary browser downloads, so the file is written server-side instead.
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", str(Path.home() / "Downloads")))
