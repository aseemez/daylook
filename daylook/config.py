"""Central configuration: environment loading, storage paths, model constants.

Everything that needs a path or a model name imports it from here so there's a single
source of truth. Paths default to ``~/.daylook`` but can be relocated with the
``DAYLOOK_HOME`` env var (useful for tests and eval, which point at a temp dir).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root if present. Dev/test runs authenticate with a free-tier
# Gemini Developer API key (GOOGLE_API_KEY); the legacy Vertex/ADC routing vars are still
# honored if a machine sets them instead.
#
# Skip this entirely in a frozen (PyInstaller) app: load_dotenv() searches *up the filesystem*
# from this module, so a bundle that happens to sit under a directory containing a .env would
# silently inherit it. The packaged app's environment is owned by the launcher — never a .env.
if not getattr(sys, "frozen", False):
    load_dotenv()


def _home() -> Path:
    """Root of Daylook's local data. Read fresh so tests can override via env."""
    override = os.environ.get("DAYLOOK_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".daylook"
    return base


# --- Storage paths (computed on access so DAYLOOK_HOME overrides always win) ---

def home() -> Path:
    return _home()


def pending_dir() -> Path:
    return _home() / "pending"


def processed_dir() -> Path:
    return _home() / "processed"


def cache_dir() -> Path:
    return _home() / "cache"


def sqlite_path() -> Path:
    return _home() / "journal.sqlite"


def log_path() -> Path:
    return _home() / "daylook.log"


def capture_health_path() -> Path:
    """Status file the capture loop writes so the app/UI can tell whether capture can
    actually SEE the screen (macOS silently revokes Screen Recording when the app's code
    signature changes — e.g. after an update — and capture then records only the wallpaper)."""
    return _home() / "capture_health.json"


def ensure_dirs() -> None:
    """Create the local data directories if they don't exist. Safe to call repeatedly."""
    for d in (pending_dir(), processed_dir(), cache_dir()):
        d.mkdir(parents=True, exist_ok=True)


# --- Model configuration ---
# Default everywhere is flash. Only bump a specific step to pro if it demonstrably fails.
DEFAULT_MODEL = os.environ.get("DAYLOOK_MODEL", "gemini-2.5-flash")
# Fallback if credits get tight — swap Analyst's model, don't change architecture.
LITE_MODEL = "gemini-2.5-flash-lite"

# Image handling for the Analyst (the only agent that receives images).
IMAGE_MAX_EDGE = 1024   # px on the long edge before sending to the model
IMAGE_JPEG_QUALITY = 70

# --- Capture cadence ---
# Capture is hybrid: an event fires on every debounced context switch (the semantic
# boundary), plus a heartbeat every HEARTBEAT_SECONDS while the user stays stably in the
# same window. Switches carry the meaning; heartbeats carry the time — without them a
# 2-hour focus session is one event and gets credited MAX_EVENT_DWELL_SECONDS at most.
HEARTBEAT_SECONDS = 12            # emit a heartbeat this often while the window is stable

# --- Capture batching (deterministic, no reasoning) ---
# The Capture Agent chunks pending events into batches; each batch becomes exactly one
# Analyst call (cost discipline: one call per session/window, never one per event).
# A new batch starts on a long idle gap, or when a run gets too large/too long to send
# comfortably in a single vision call. Only SWITCH events count toward MAX_BATCH_EVENTS —
# heartbeats are cheap text riders (few get screenshots; see MAX_IMAGES_PER_BATCH) so a
# heartbeat-dense hour still costs one vision call, same as before heartbeats existed.
IDLE_GAP_SECONDS = 20 * 60        # gap this long ends a session
MAX_BATCH_EVENTS = 12             # cap SWITCH events per Analyst call
MAX_BATCH_TOTAL_EVENTS = 400      # absolute cap incl. heartbeats (~75 min at 12s cadence)
MAX_BATCH_SPAN_SECONDS = 60 * 60  # cap wall-clock span per batch
MAX_IMAGES_PER_BATCH = 12         # screenshots actually sent per vision call (keyframes)

# An event's "dwell" (time attributed to it) is the gap to the next event, capped here so a
# long idle gap or the final event of the day can't inflate a block's duration.
MAX_EVENT_DWELL_SECONDS = 15 * 60

# A readable journal groups the day into a handful of meaningful blocks, not a minute-by-minute
# log. Blocks shorter than this get folded into an adjacent block (a 40-second glance at Safari
# in the middle of a Terminal session shouldn't be its own timeline row). See
# analyst.merge_blocks — this is presentation granularity, not a change to captured data:
# folded events keep their source_event_ids, so every block still traces back to real capture.
MIN_BLOCK_SECONDS = 120


# --- Productivity lanes ---------------------------------------------------------------
# Each granular activity category rolls up into one of a few high-level "lanes". Lanes are
# what the calendar colours by and what focus-scoring counts, so the day reads as a handful of
# bright bands ("productive", "social", …) rather than a wall of app names. The UI mirrors this
# map (daylook/api/static/index.html) — keep them in sync if you edit either.
LANES: dict[str, str] = {
    "coding": "focus", "writing": "focus", "review": "focus", "planning": "focus", "meeting": "focus",
    "research": "research",
    "communication": "comms",
    "distraction": "social", "social": "social", "entertainment": "social",
    "break": "idle", "other": "idle",
}
# focus/research/comms count as productive; social is a distraction; idle is neutral.
LANE_PRODUCTIVITY: dict[str, str] = {
    "focus": "productive", "research": "productive", "comms": "productive",
    "social": "distracting", "idle": "neutral",
}


def lane_of(category: str | None) -> str:
    """The productivity lane a granular category belongs to (default 'idle')."""
    return LANES.get((category or "other").lower(), "idle")


def productivity_of(category: str | None) -> str:
    """'productive' | 'distracting' | 'neutral' for a category."""
    return LANE_PRODUCTIVITY.get(lane_of(category), "neutral")


def vertex_configured() -> bool:
    """True when the Vertex routing env vars are present (legacy/relay-server path)."""
    return (
        os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        and bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
    )


def model_configured() -> bool:
    """True when the process can reach a Gemini model by *some* route.

    Two routes exist: a Gemini Developer API key in GOOGLE_API_KEY (dev free tier, a user's
    own key, or a relay invite token — the SDK treats all three the same), or Vertex + ADC
    (legacy; kept because the deployed relay still authenticates upstream this way).
    """
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    return (bool(key) and "PASTE" not in key) or vertex_configured()
