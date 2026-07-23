"""Root Agent (build step 6) — orchestration + session memory.

The user-facing chat. An ADK ``LlmAgent`` that routes each message to the right tool:

  * **process the day** — run the capture->analyst->journalist->memory pipeline,
  * **show today's journal**, **answer questions about past days**, and
  * **remember a preference** for the rest of this conversation.

Session refinements (e.g. "exclude Slack from focus time") live in ADK session state via the
Runner's session service — scoped to one chat, never written to SQLite — and are threaded into
the Journalist / Memory prompts so later responses in the session honor them (CLAUDE.md's
"session memory" concept, kept distinct from long-term SQLite memory).

Heavy pipeline work (which itself makes model calls) is dispatched to a worker thread so it
never nests inside the Root's own event loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import date as date_cls
from pathlib import Path

from google.adk.tools import ToolContext  # module-level so ADK can resolve tool type hints

from daylook import config
from daylook.agents.memory import MemoryAgent
from daylook.pipeline import Pipeline

APP_NAME = "daylook"
USER_ID = "local-user"


class RootAgent:
    def __init__(
        self,
        memory: MemoryAgent | None = None,
        pipeline: Pipeline | None = None,
        pending_dir: Path | None = None,
    ):
        self.memory = memory if memory is not None else MemoryAgent()
        self.pipeline = pipeline if pipeline is not None else Pipeline(self.memory)
        self.pending_dir = pending_dir
        # In-process capture: a background thread runs the real-capture loop *inside this app
        # process* (see start_capture_process / stop_capture_process), stopped cooperatively via
        # the Event. Running it here — rather than spawning a child — means the screen recording
        # is done by the same process the user already granted macOS Screen Recording to, so
        # macOS doesn't treat capture as a new client and re-prompt on every launch. None/idle
        # until a capture is running.
        self._capture_thread: threading.Thread | None = None
        self._capture_stop: threading.Event | None = None
        self._capture_started_at: float | None = None  # epoch secs, for the live "recording" UI
        self._runner = self._build_runner()

    # --- tools (closures over self) ---

    def _build_agent(self):
        from google.adk.agents import LlmAgent

        pipeline = self.pipeline
        memory = self.memory
        pending_dir = self.pending_dir
        root = self

        async def process_today(tool_context: ToolContext) -> dict:
            """Run the full pipeline over pending capture events and write today's journal.
            Honors any preferences the user has set this session. Returns the writeup."""
            refinements = list(tool_context.state.get("refinements", []))
            result = await asyncio.to_thread(
                pipeline.process_day, pending_dir=pending_dir, refinements=refinements
            )
            tool_context.state["current_date"] = result["date"]
            tool_context.state["current_day"] = result["day"]
            tool_context.state["current_writeup"] = result["writeup"]
            return {
                "date": result["date"],
                "num_events": result["num_events"],
                "num_blocks": result["num_blocks"],
                "writeup": result["writeup"],
                # A backlog (app closed for a few days) is processed per day; each earlier
                # day got its own journal too. Lets the assistant tell the user that
                # instead of silently folding three days into one.
                "also_processed_dates": [d["date"] for d in result.get("days", [])[:-1]],
            }

        def show_today(tool_context: ToolContext) -> dict:
            """Return the journal already generated this session, if any."""
            writeup = tool_context.state.get("current_writeup")
            if not writeup:
                return {"writeup": None, "note": "No journal yet this session — process the day first."}
            return {"date": tool_context.state.get("current_date"), "writeup": writeup}

        async def remember_preference(preference: str, tool_context: ToolContext) -> dict:
            """Record a session preference (e.g. 'exclude Slack from focus time') and, if a
            journal exists this session, regenerate it so the preference is reflected."""
            refinements = list(tool_context.state.get("refinements", []))
            refinements.append(preference)
            tool_context.state["refinements"] = refinements

            note = "Preference noted; it will shape future responses this session."
            day = tool_context.state.get("current_day")
            if day and day.get("blocks"):
                from daylook.agents.journalist import write_journal
                writeup = await asyncio.to_thread(write_journal, day, refinements)
                tool_context.state["current_writeup"] = writeup
                note = "Preference noted and today's journal updated to honor it."
            return {"refinements": refinements, "note": note}

        async def ask_history(question: str, tool_context: ToolContext) -> dict:
            """Answer a question about PAST days by reading long-term memory (SQLite)."""
            refinements = list(tool_context.state.get("refinements", []))
            today = date_cls.today().isoformat()
            answer = await asyncio.to_thread(
                memory.answer, question, today, refinements
            )
            return {"answer": answer}

        def start_capture(minutes: int = 0) -> dict:
            """Begin REAL macOS screen capture in the background. This RECORDS THE USER'S
            ACTUAL SCREEN, so only call it when the user clearly asks to start tracking or
            capturing. Pass minutes if they name a duration (0 = run until they ask to stop).
            Events are written to the pending folder; afterward call process_today to build the
            journal from them."""
            return root.start_capture_process(minutes=minutes)

        def stop_capture() -> dict:
            """Stop the background screen capture previously started with start_capture."""
            return root.stop_capture_process()

        instruction = (
            "You are Daylook, a personal work-journal assistant. You help the user turn "
            "their screen activity into a readable daily timeline and answer questions about "
            "their work.\n\n"
            "Routing:\n"
            "- If the user wants to process/generate today's journal or asks what they did "
            "today, call process_today, then present the returned 'writeup' to the user "
            "VERBATIM (do not summarize or drop the timeline or numbers). If "
            "'also_processed_dates' is non-empty, briefly mention those earlier days were "
            "caught up and journaled too (they're in the sidebar).\n"
            "- If they ask to see the journal again, call show_today.\n"
            "- If they ask about a PAST day or overall history, call ask_history and relay the "
            "answer.\n"
            "- If they state a preference or exclusion (e.g. 'don't count Slack as focus', "
            "'group by hour'), call remember_preference with their exact preference, then "
            "confirm it's applied.\n"
            "- If they ask to START tracking or capturing their screen (e.g. 'start tracking', "
            "'begin capture', 'record my screen'), call start_capture. This starts REAL screen "
            "recording, so only do it on a clear request; pass minutes if they name a duration. "
            "Tell them capture has started.\n"
            "- If they ask to STOP tracking/capturing, call stop_capture and simply confirm that "
            "capture has stopped. Do NOT build, process, or summarize the day here — never call "
            "process_today off the back of a stop request. Wait until the user explicitly asks "
            "for their day (e.g. 'what did I do today'), then process_today runs on its own.\n"
            "Be concise and friendly. Never invent activity or numbers."
        )

        return LlmAgent(
            name="root",
            model=config.DEFAULT_MODEL,
            instruction=instruction,
            tools=[
                process_today, show_today, remember_preference, ask_history,
                start_capture, stop_capture,
            ],
        )

    # --- real-capture process control (drives the "dumb" capture layer as a subprocess) ---

    def _capture_running(self) -> bool:
        return self._capture_thread is not None and self._capture_thread.is_alive()

    def start_capture_process(self, minutes: int = 0, dry_run: bool = False) -> dict:
        """Start the real macOS capture loop on a background thread **in this process**.

        **Consent-gated:** unless ``dry_run``, this records the user's actual screen. Events are
        written to ``pending/`` exactly as the terminal command would. ``minutes<=0`` runs until
        :meth:`stop_capture_process` is called; a positive value auto-stops after that long.
        ``dry_run`` detects window switches but takes no screenshots — safe to run without
        capturing screen content (used by the tests). Idempotent: a second start while one is
        already running is a no-op.

        Why in-thread rather than a subprocess: macOS grants Screen Recording per *process
        identity*. The app already holds that grant; a spawned child looks like a brand-new
        capture client to macOS and gets re-prompted every launch. Recording from inside the
        granted app process is what makes the permission stick.
        """
        if self._capture_running():
            return {"status": "already_running"}

        from daylook.capture_script import real_capture

        config.ensure_dirs()
        stop = threading.Event()
        duration = (minutes * 60) if (minutes and minutes > 0) else None

        def _loop() -> None:
            try:
                real_capture.run(
                    pending=self.pending_dir,
                    dry_run=dry_run,
                    duration_seconds=duration,
                    stop_event=stop,
                )
            except Exception as e:  # never let a capture error take down the app
                from daylook.observability import observe
                with observe("root", "capture_error", str(e)[:120]):
                    pass

        self._capture_stop = stop
        self._capture_thread = threading.Thread(target=_loop, name="daylook-capture", daemon=True)
        self._capture_thread.start()
        self._capture_started_at = time.time()
        return {
            "status": "started",
            "minutes": minutes or None,
            "dry_run": dry_run,
            "note": "Screen capture is running in the background. Events land in pending/. "
                    "When done, call stop_capture, then process_today to build the journal.",
        }

    def stop_capture_process(self) -> dict:
        """Stop the in-process capture thread. Idempotent: ``not_running`` if idle.

        Sets the cooperative stop Event and joins; the loop exits within one poll interval,
        after finishing any in-flight write, so no already-captured event is lost.
        """
        if not self._capture_running():
            self._capture_thread = None
            self._capture_stop = None
            self._capture_started_at = None
            return {"status": "not_running"}

        if self._capture_stop is not None:
            self._capture_stop.set()
        self._capture_thread.join(timeout=10)
        self._capture_thread = None
        self._capture_stop = None
        self._capture_started_at = None
        return {"status": "stopped"}

    def capture_status(self) -> dict:
        """Whether a background capture is running, for how long (seconds), and whether it can
        actually SEE the screen (see ``real_capture.write_health`` — a revoked Screen Recording
        grant silently turns capture into wallpaper shots; the UI must be able to warn)."""
        running = self._capture_running()
        elapsed = (
            int(time.time() - self._capture_started_at)
            if running and self._capture_started_at is not None else 0
        )
        from daylook.capture_script.real_capture import read_health
        health = read_health()
        return {
            "running": running,
            "elapsed_seconds": elapsed,
            "health": health,
            # convenience flag for the UI: only warn about a CURRENT capture — a stale health
            # file from a long-finished run shouldn't nag after the app was fixed/re-granted
            "blind": bool(running and health and health.get("blind")),
        }

    def build_llm_agent(self):
        """Return a fresh underlying ``LlmAgent`` (tools included) for an external runner.

        The FastAPI path uses ``chat()``, which drives this agent through the Root's own
        ``Runner``. ``adk web`` instead wants a module-level ``root_agent`` it can drive with
        its OWN runner/session service — this exposes exactly that same agent to it. See
        ``adk_web/daylook_agent/agent.py``.
        """
        return self._build_agent()

    def _build_runner(self):
        from google.adk.runners import InMemoryRunner
        agent = self._build_agent()
        return InMemoryRunner(agent=agent, app_name=APP_NAME)

    # --- chat entry point ---

    async def _ensure_session(self, session_id: str) -> None:
        svc = self._runner.session_service
        existing = await svc.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )
        if existing is None:
            await svc.create_session(
                app_name=APP_NAME, user_id=USER_ID, session_id=session_id,
                state={"refinements": []},
            )

    async def chat(self, message: str, session_id: str | None = None) -> tuple[str, str]:
        """Handle one user turn. Returns (reply, session_id). Reuses session state by id."""
        from google.genai import types

        session_id = session_id or uuid.uuid4().hex
        await self._ensure_session(session_id)

        from daylook.observability import observe

        content = types.Content(role="user", parts=[types.Part(text=message)])
        reply = ""
        with observe("root", "chat", message[:80], session_id=session_id) as rec:
            async for ev in self._runner.run_async(
                user_id=USER_ID, session_id=session_id, new_message=content
            ):
                if ev.is_final_response() and ev.content and ev.content.parts:
                    reply = ev.content.parts[0].text or reply
            rec["output"] = f"{len(reply)} chars"
        return reply, session_id

    async def get_refinements(self, session_id: str) -> list[str]:
        """Inspect the refinements stored for a session (used by tests / debugging)."""
        sess = await self._runner.session_service.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )
        return list(sess.state.get("refinements", [])) if sess else []
