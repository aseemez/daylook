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

import os
import threading
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


# --- Hourly auto-journal -----------------------------------------------------------------
#
# The journal used to be built only when the user happened to ask about their day — capture
# could run for 14 hours and the day stayed invisible until a chat prompt kicked the pipeline
# (and one hung model call could stall even that). Instead, a background thread processes
# whatever is pending on a fixed cadence, so the journal is always at most an hour behind and
# a "what did I do today" answers instantly from (nearly) current data. The chat path still
# triggers processing too; the pipeline's process-wide lock keeps the two from colliding.
#
# 0 disables (tests, dev shells that don't want surprise model calls). First catch-up pass
# runs a minute after boot so a multi-day backlog starts clearing without any prompt.
AUTO_JOURNAL_SECONDS = int(os.environ.get("DAYLOOK_AUTO_JOURNAL_SECONDS", "3600"))
_auto_journal_stop = threading.Event()
_auto_journal_thread: threading.Thread | None = None


def _auto_journal_once() -> None:
    """One catch-up pass: process pending events if there are any and a model is reachable.

    Builds its own MemoryAgent/Pipeline (own SQLite connection) — this runs on a thread, and
    sharing the request handlers' connection would race them. Never raises: a failed pass
    (offline, model timeout) leaves events pending and simply tries again next tick.
    """
    from daylook.observability import observe
    from daylook.pipeline import Pipeline

    try:
        if not config.model_configured():
            return
        if CaptureAgent().pending_count() == 0:
            return
        memory = MemoryAgent()
        try:
            with observe("pipeline", "auto_journal") as rec:
                result = Pipeline(memory).process_day()
                rec["output"] = f"journaled {result.get('date') or 'nothing'}"
            _import_screentime(memory)
        finally:
            memory.close()
    except Exception as e:
        with observe("pipeline", "auto_journal_error", str(e)[:120]):
            pass
    _prune_processed()


def _import_screentime(memory: MemoryAgent) -> None:
    """Fold cross-device (iPhone/iPad) Screen Time usage into the journal, if available.

    A no-op until the user grants Full Disk Access + enables Share Across Devices (the reader
    reports ``available=False`` and we stay quiet). Best-effort — never blocks journaling.
    """
    from daylook.capture_script.screentime_import import import_screentime
    from daylook.observability import observe
    try:
        result = import_screentime(memory)
    except Exception:
        return
    if result.get("available"):
        with observe("screentime", "import") as rec:
            rec["output"] = f"{result['imported']} block(s) over {len(result['days'])} day(s)"


def _prune_processed() -> None:
    """Age out raw event files (screenshots!) from processed/ past the retention window.

    Long-session hygiene: a >10h/day user writes ~250 MB of screenshots a day, and keeping
    them forever is both a disk leak and a privacy liability. The journal itself (SQLite,
    including source_event_ids) is never touched — only the raw JPEG/JSON pairs age out.
    Best-effort by design: a locked file just waits for the next pass.
    """
    days = config.PROCESSED_RETENTION_DAYS
    if days <= 0:
        return
    import time
    cutoff = time.time() - days * 86400
    try:
        for f in config.processed_dir().iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _auto_journal_loop() -> None:
    if _auto_journal_stop.wait(60):  # boot catch-up after a minute, unless shutting down
        return
    _auto_journal_once()
    while not _auto_journal_stop.wait(AUTO_JOURNAL_SECONDS):
        _auto_journal_once()


@app.on_event("startup")
def _start_auto_journal() -> None:
    global _auto_journal_thread
    if AUTO_JOURNAL_SECONDS <= 0 or (
        _auto_journal_thread is not None and _auto_journal_thread.is_alive()
    ):
        return
    _auto_journal_stop.clear()
    _auto_journal_thread = threading.Thread(
        target=_auto_journal_loop, name="daylook-auto-journal", daemon=True
    )
    _auto_journal_thread.start()


@app.on_event("shutdown")
def _stop_auto_journal() -> None:
    _auto_journal_stop.set()


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


class SettingsPatch(BaseModel):
    """Ambient-capture preferences (Phase 1). Any omitted field is left unchanged."""
    auto_start_capture: bool | None = None
    launch_at_login: bool | None = None


@app.get("/settings")
def get_settings() -> dict:
    """The ambient-capture toggles for the Settings UI / menubar."""
    d = settings.load()
    return {
        "auto_start_capture": bool(d.get("auto_start_capture")),
        "launch_at_login": bool(d.get("launch_at_login")),
    }


@app.post("/settings")
def post_settings(req: SettingsPatch) -> dict:
    """Persist the ambient toggles and apply side effects.

    ``launch_at_login`` is pushed to the OS immediately (login-item register/unregister);
    ``auto_start_capture`` is only read at the next launch, so it just persists here.
    """
    patch: dict = {}
    if req.auto_start_capture is not None:
        patch["auto_start_capture"] = bool(req.auto_start_capture)
    if req.launch_at_login is not None:
        patch["launch_at_login"] = bool(req.launch_at_login)
    data = settings.save(patch)
    login_applied = None
    if req.launch_at_login is not None:
        from daylook import loginitem
        login_applied = loginitem.set_login_item(bool(req.launch_at_login))
    return {
        "auto_start_capture": bool(data.get("auto_start_capture")),
        "launch_at_login": bool(data.get("launch_at_login")),
        "login_item_applied": login_applied,
    }


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
    detail: str | None = None
    category: str | None = None
    app_name: str | None = None


class BlockPatch(BaseModel):
    start: str | None = None
    end: str | None = None
    label: str | None = None
    detail: str | None = None
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
        duration_seconds=dur, detail=(req.detail or "").strip(),
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
    if req.detail is not None:
        fields["detail"] = req.detail.strip()
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


@app.post("/capture/pause")
def capture_pause(root: RootAgent = Depends(get_root)) -> dict:
    """Hold recording without ending the session — nothing is captured until resume."""
    return root.pause_capture_process()


@app.post("/capture/resume")
def capture_resume(root: RootAgent = Depends(get_root)) -> dict:
    """Resume a paused recording."""
    return root.resume_capture_process()


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
