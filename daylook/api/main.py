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

from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
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
    """Lazy Memory handle for the journal endpoints (reads + manual calendar edits).

    Deliberately separate from the Root Agent: the journal pane just needs to read stored days
    and apply hand edits (no model, no chat session), so it should not spin up the whole
    orchestration graph. Same clean Memory interface, so this stays true if Memory is ever split
    into its own service.
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


@app.get("/journal/backlog")
def journal_backlog() -> dict:
    """Pending capture events grouped by calendar date — the 'you have unprocessed days'
    signal. A backlog builds up whenever capture ran but processing didn't (the app was
    quit, updated, or the user just never asked); it used to sit invisible until a chat
    happened to trigger the pipeline. The UI calls this once at boot and nudges the user
    when days other than today are waiting."""
    try:
        events = CaptureAgent().read_pending()
    except Exception:
        events = []
    by_date: dict[str, int] = {}
    for e in events:
        d = e.dt.date().isoformat()
        by_date[d] = by_date.get(d, 0) + 1
    return {
        "days": [{"date": d, "events": n} for d, n in sorted(by_date.items())],
        "total": len(events),
    }


@app.get("/journal/dates")
def journal_dates(memory: MemoryAgent = Depends(get_memory)) -> dict:
    """Every date with a journal or hand-added blocks, newest first — the sidebar's day list."""
    return {"dates": sorted(memory.content_dates(), reverse=True)}


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


class BlockIn(BaseModel):
    """A user-authored calendar entry. Times are wall-clock ``HH:MM`` on the day being edited;
    the server anchors them to that date and computes the duration."""
    start: str            # "HH:MM"
    end: str              # "HH:MM"
    label: str
    category: str | None = None
    app_name: str | None = None


class BlockPatch(BaseModel):
    start: str | None = None
    end: str | None = None
    label: str | None = None
    category: str | None = None
    app_name: str | None = None


def _resolve_times(date: str, start: str, end: str) -> tuple[str, str, int]:
    """Turn a date + two ``HH:MM`` strings into ISO timestamps and a positive duration.

    Raises 400 on malformed times or a non-positive span so the UI can show a clear error
    instead of writing a nonsensical block.
    """
    try:
        s = datetime.strptime(f"{date} {start}", "%Y-%m-%d %H:%M")
        e = datetime.strptime(f"{date} {end}", "%Y-%m-%d %H:%M")
    except ValueError:
        raise HTTPException(status_code=400, detail="Times must be HH:MM (24-hour).")
    dur = int((e - s).total_seconds())
    if dur <= 0:
        raise HTTPException(status_code=400, detail="End time must be after start time.")
    return s.isoformat(timespec="seconds"), e.isoformat(timespec="seconds"), dur


@app.post("/journal/{date}/blocks")
def add_block(date: str, req: BlockIn, memory: MemoryAgent = Depends(get_memory)) -> dict:
    """Add a manual activity to a day's calendar (e.g. an offline meeting)."""
    start, end, dur = _resolve_times(date, req.start, req.end)
    block = memory.add_block(
        date=date, start=start, end=end, label=req.label.strip() or "Activity",
        app_name=(req.app_name or "").strip(), category=(req.category or "other"),
        duration_seconds=dur,
    )
    return {"block": block}


@app.patch("/journal/{date}/blocks/{block_id}")
def edit_block(
    date: str, block_id: int, req: BlockPatch, memory: MemoryAgent = Depends(get_memory)
) -> dict:
    """Edit one block (label/category/times). Works on both manual and captured blocks."""
    existing = memory.store.get_block(block_id)
    if not existing or existing["date"] != date:
        raise HTTPException(status_code=404, detail="No such block on this day.")
    fields: dict = {}
    if req.label is not None:
        fields["label"] = req.label.strip() or existing["label"]
    if req.category is not None:
        fields["category"] = req.category
    if req.app_name is not None:
        fields["app_name"] = req.app_name.strip()
    if req.start is not None or req.end is not None:
        # Re-derive both timestamps + duration together so start/end/duration stay consistent.
        start = req.start if req.start is not None else existing["start"][11:16]
        end = req.end if req.end is not None else existing["end"][11:16]
        fields["start"], fields["end"], fields["duration_seconds"] = _resolve_times(date, start, end)
    block = memory.update_block(block_id, **fields)
    return {"block": block}


class Reclassify(BaseModel):
    ids: list[int]
    category: str


@app.post("/journal/{date}/reclassify")
def reclassify(date: str, req: Reclassify, memory: MemoryAgent = Depends(get_memory)) -> dict:
    """Correct the AI's productivity guess for a whole band: set several blocks' category at once
    (the lane, and thus the colour and focus-scoring, follows from category). Lets the user say
    'that was actually work / that was social' in one tap."""
    updated = 0
    for bid in req.ids:
        b = memory.store.get_block(bid)
        if b and b["date"] == date:
            memory.store.update_block(bid, category=req.category)
            updated += 1
    return {"updated": updated}


@app.delete("/journal/{date}/blocks/{block_id}")
def remove_block(date: str, block_id: int, memory: MemoryAgent = Depends(get_memory)) -> dict:
    existing = memory.store.get_block(block_id)
    if not existing or existing["date"] != date:
        raise HTTPException(status_code=404, detail="No such block on this day.")
    memory.delete_block(block_id)
    return {"deleted": block_id}


class CaptureStart(BaseModel):
    minutes: int = 0  # 0 = run until stopped


@app.get("/capture/status")
def capture_status(root: RootAgent = Depends(get_root)) -> dict:
    """Is screen capture running, and for how long — drives the always-visible recording pill."""
    return root.capture_status()


@app.post("/capture/start")
def capture_start(req: CaptureStart, root: RootAgent = Depends(get_root)) -> dict:
    """Begin REAL screen capture. This is an explicit, user-initiated consent action (the user
    clicked Start), the same gate as the chat 'start tracking' command."""
    return root.start_capture_process(minutes=req.minutes)


@app.post("/capture/stop")
def capture_stop(root: RootAgent = Depends(get_root)) -> dict:
    """Stop screen capture. Always available so the user can halt recording in one click."""
    return root.stop_capture_process()


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
