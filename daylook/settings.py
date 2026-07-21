"""User settings — the small, persisted choices that decide *how* the app reaches its AI.

Separate from ``config.py`` on purpose: ``config`` is developer-facing constants and storage
paths; this is the end user's runtime choices, saved to ``<home>/settings.json`` and edited from
the setup card in the web UI (POST /setup). It carries no secrets that aren't the user's own.

Two "how do I reach a model" modes, so the money-later migration is a setting, not a rewrite:

  * ``relay``   — the default for invited users. We send the invite *token* as ``GOOGLE_API_KEY``
                  and point the genai SDK at the relay via ``GOOGLE_GEMINI_BASE_URL``. The relay
                  holds the real Google credentials; nothing sensitive ships in the app.
  * ``own_key`` — a user who brings their own (free) AI Studio key. Same ``GOOGLE_API_KEY`` seam,
                  no base-URL override, so calls go straight to Google on their own quota.

``apply_to_env`` is the whole point: it translates a saved settings dict into the exact
environment the google-genai SDK reads when it lazily builds its client. The launcher calls it
before the server starts; POST /setup calls it again so a code entered mid-session takes effect
without a restart (ADK builds the client lazily on the first model call).
"""

from __future__ import annotations

import json
import os

from daylook import config

# The deployed relay URL — the Cloud Run service that holds the real Google credentials and
# fans out to Vertex for invited users. Baked in as the default so the packaged app works with
# nothing but an invite token; override per-machine with DAYLOOK_RELAY_URL (e.g. to point a
# dev build at a staging relay). Only consulted in "relay" mode.
DEFAULT_RELAY_URL = os.environ.get(
    "DAYLOOK_RELAY_URL",
    "https://contextline-relay-862472415362.us-central1.run.app",
)

_DEFAULTS: dict = {
    "api_key_mode": "relay",   # "relay" | "own_key"
    "invite_token": "",        # the invite token (relay) or the user's own AI Studio key
    "relay_url": DEFAULT_RELAY_URL,
}


def settings_path():
    return config.home() / "settings.json"


def load() -> dict:
    """Current settings, defaults filled in for anything missing or unreadable."""
    data = dict(_DEFAULTS)
    path = settings_path()
    if path.exists():
        try:
            data.update(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            pass  # corrupt/unreadable file -> fall back to defaults rather than crash the app
    return data


def save(patch: dict) -> dict:
    """Merge ``patch`` into the saved settings and write them back. Returns the merged result."""
    data = load()
    data.update({k: v for k, v in patch.items() if k in _DEFAULTS})
    config.home().mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(data, indent=2))
    return data


def is_configured(data: dict | None = None) -> bool:
    """True once the user has supplied a token/key — the setup card hides when this is true."""
    data = data if data is not None else load()
    return bool(data.get("invite_token", "").strip())


def apply_to_env(data: dict | None = None) -> bool:
    """Set the process env the google-genai SDK reads. Returns True if a token was applied.

    Idempotent and safe to call before the SDK client exists (ADK builds it lazily). We always
    force ``GOOGLE_GENAI_USE_VERTEXAI=FALSE`` here: the packaged app is a Gemini-Developer-API
    client (token + optional base-URL), never Vertex, and the explicit FALSE also blocks a stray
    repo ``.env`` from flipping it back to TRUE in dev.
    """
    data = data if data is not None else load()
    token = data.get("invite_token", "").strip()
    if not token:
        return False

    os.environ["GOOGLE_API_KEY"] = token
    os.environ["DAYLOOK_USE_MCP"] = "0"   # packaged app uses the plain filesystem read path
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"

    if data.get("api_key_mode", "relay") == "relay":
        relay = (data.get("relay_url") or DEFAULT_RELAY_URL).strip()
        if relay:
            os.environ["GOOGLE_GEMINI_BASE_URL"] = relay
    else:  # own_key: talk straight to Google, no relay indirection
        os.environ.pop("GOOGLE_GEMINI_BASE_URL", None)
    return True
