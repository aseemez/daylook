"""FastAPI wrapper (build step 9) — minimal, local-only, dev-only (no auth).

Two endpoints over the Root Agent:
  * ``POST /chat``  — ``{"message": str, "session_id": str | null}`` -> ``{"reply", "session_id"}``.
    A null ``session_id`` starts a new ADK session and returns its id; reusing the id keeps the
    session's refinements. Synchronous (no streaming in v1 — can be added later without touching
    the agent layer).
  * ``GET /health`` — 200 with a small status payload.

Run locally:
    uvicorn daylook.api.main:app --reload
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from daylook import config, settings
from daylook.agents.capture import CaptureAgent
from daylook.agents.memory import MemoryAgent
from daylook.agents.root import RootAgent

# Resolved relative to this module so it works both from a normal install and
# from a frozen (PyInstaller) bundle, where package data sits next to the .pyc.
_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Daylook", version="0.1.0")

_root: RootAgent | None = None


def get_root() -> RootAgent:
    """Lazily build a single Root Agent so ADK session state persists across requests."""
    global _root
    if _root is None:
        _root = RootAgent()
    return _root


_memory: MemoryAgent | None = None


def get_memory() -> MemoryAgent:
    """Lazy read-only Memory handle for the journal endpoints.

    Deliberately separate from the Root Agent: the sidebar just needs to *read* stored days
    (no model, no session), so it should not spin up the whole orchestration graph. Same clean
    Memory interface, so this stays true if Memory is ever split into its own service.
    """
    global _memory
    if _memory is None:
        _memory = MemoryAgent()
    return _memory


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """Serve the chat page (single self-contained HTML file, no CDN)."""
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, root: RootAgent = Depends(get_root)) -> ChatResponse:
    reply, session_id = await root.chat(req.message, req.session_id)
    return ChatResponse(reply=reply, session_id=session_id)


class SetupRequest(BaseModel):
    invite_token: str
    # Optional overrides — the UI sends only the token; these let a bring-your-own-key
    # user (or a future settings screen) pick the mode / relay without a rebuild.
    api_key_mode: str | None = None
    relay_url: str | None = None


@app.get("/setup")
def get_setup() -> dict:
    """Whether the app has a token/key yet — the UI shows its setup card when not configured."""
    data = settings.load()
    return {"configured": settings.is_configured(data), "mode": data.get("api_key_mode")}


@app.post("/setup")
def post_setup(req: SetupRequest) -> dict:
    """Save the invite code (or own key) and apply it to the live process.

    Applying to the environment here is what makes a code entered mid-session work without a
    restart: ADK builds its model client lazily on the first /chat, and by then these env vars
    are set. Safe as long as no model call has happened yet — the first-run flow guarantees it.
    """
    patch = {"invite_token": req.invite_token.strip()}
    if req.api_key_mode:
        patch["api_key_mode"] = req.api_key_mode
    if req.relay_url is not None:
        patch["relay_url"] = req.relay_url.strip()
    data = settings.save(patch)
    settings.apply_to_env(data)
    return {"configured": settings.is_configured(data), "mode": data.get("api_key_mode")}


@app.get("/journal/dates")
def journal_dates(memory: MemoryAgent = Depends(get_memory)) -> dict:
    """Every date that has a saved journal, newest first — the sidebar's day list."""
    return {"dates": sorted(memory.available_dates(), reverse=True)}


@app.get("/journal/{date}")
def journal_day(date: str, memory: MemoryAgent = Depends(get_memory)) -> dict:
    """One day's stored journal for the journal pane: writeup + blocks + distributions.

    Returns 200 with ``writeup: null`` (rather than 404) when the day isn't stored yet, so the
    UI can render an empty-state without treating it as an error.
    """
    day = memory.get_journal(date)
    store = memory.store
    return {
        "date": date,
        "writeup": day.get("writeup") if day else None,
        "created_at": day.get("created_at") if day else None,
        "blocks": store.get_blocks(date),
        "time_by_category": store.time_by_category(date),
        "time_by_app": store.time_by_app(date),
    }


@app.get("/health")
def health() -> dict:
    try:
        pending = CaptureAgent().pending_count()
    except Exception:
        pending = None
    return {
        "status": "ok",
        "model_configured": config.model_configured(),
        "model": config.DEFAULT_MODEL,
        "pending_events": pending,
    }
