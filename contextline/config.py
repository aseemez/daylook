"""Central configuration: environment loading, storage paths, model constants.

Everything that needs a path or a model name imports it from here so there's a single
source of truth. Paths default to ``~/.contextline`` but can be relocated with the
``CONTEXTLINE_HOME`` env var (useful for tests and eval, which point at a temp dir).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the repo root if present. Real credentials come from ADC, not here;
# .env only carries the Vertex routing vars (project, location, USE_VERTEXAI).
#
# Skip this entirely in a frozen (PyInstaller) app: load_dotenv() searches *up the filesystem*
# from this module, so a bundle that happens to sit under a directory containing a .env would
# silently inherit it. The packaged app's environment is owned by the launcher — never a .env.
if not getattr(sys, "frozen", False):
    load_dotenv()


def _home() -> Path:
    """Root of Contextline's local data. Read fresh so tests can override via env."""
    override = os.environ.get("CONTEXTLINE_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".contextline"
    return base


# --- Storage paths (computed on access so CONTEXTLINE_HOME overrides always win) ---

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
    return _home() / "contextline.log"


def ensure_dirs() -> None:
    """Create the local data directories if they don't exist. Safe to call repeatedly."""
    for d in (pending_dir(), processed_dir(), cache_dir()):
        d.mkdir(parents=True, exist_ok=True)


# --- Model configuration ---
# Default everywhere is flash. Only bump a specific step to pro if it demonstrably fails.
DEFAULT_MODEL = os.environ.get("CONTEXTLINE_MODEL", "gemini-2.5-flash")
# Fallback if credits get tight — swap Analyst's model, don't change architecture.
LITE_MODEL = "gemini-2.5-flash-lite"

# Image handling for the Analyst (the only agent that receives images).
IMAGE_MAX_EDGE = 1024   # px on the long edge before sending to the model
IMAGE_JPEG_QUALITY = 70

# --- Capture batching (deterministic, no reasoning) ---
# The Capture Agent chunks pending events into batches; each batch becomes exactly one
# Analyst call (cost discipline: one call per session/window, never one per event).
# A new batch starts on a long idle gap, or when a run gets too large/too long to send
# comfortably in a single vision call.
IDLE_GAP_SECONDS = 20 * 60        # gap this long ends a session
MAX_BATCH_EVENTS = 12             # cap events per Analyst call
MAX_BATCH_SPAN_SECONDS = 60 * 60  # cap wall-clock span per batch

# An event's "dwell" (time attributed to it) is the gap to the next event, capped here so a
# long idle gap or the final event of the day can't inflate a block's duration.
MAX_EVENT_DWELL_SECONDS = 15 * 60


def vertex_configured() -> bool:
    """True when the Vertex routing env vars are present."""
    return (
        os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").upper() == "TRUE"
        and bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
    )
