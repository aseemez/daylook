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
import signal
import subprocess
import sys
import uuid
from datetime import date as date_cls
from pathlib import Path

from google.adk.tools import ToolContext  # module-level so ADK can resolve tool type hints

from contextline import config
from contextline.agents.memory import MemoryAgent
from contextline.pipeline import Pipeline

APP_NAME = "contextline"
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
        # Handle to a background real-capture subprocess, if one is running (see
        # start_capture_process / stop_capture_process). None when idle.
        self._capture_proc: subprocess.Popen | None = None
        self._capture_log = None
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
                from contextline.agents.journalist import write_journal
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
            "You are Contextline, a personal work-journal assistant. You help the user turn "
            "their screen activity into a readable daily timeline and answer questions about "
            "their work.\n\n"
            "Routing:\n"
            "- If the user wants to process/generate today's journal or asks what they did "
            "today, call process_today, then present the returned 'writeup' to the user "
            "VERBATIM (do not summarize or drop the timeline or numbers).\n"
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

    def start_capture_process(self, minutes: int = 0, dry_run: bool = False) -> dict:
        """Spawn the real macOS capture script in the background and track its process.

        **Consent-gated:** unless ``dry_run``, this records the user's actual screen. Events are
        written to ``pending/`` exactly as the terminal command would. ``minutes<=0`` runs until
        :meth:`stop_capture_process` is called; a positive value auto-stops after that long.
        ``dry_run`` detects window switches but takes no screenshots — safe to run without
        capturing screen content (used by the tests). Idempotent: a second start while one is
        already running is a no-op that reports the existing pid.
        """
        if self._capture_proc is not None and self._capture_proc.poll() is None:
            return {"status": "already_running", "pid": self._capture_proc.pid}

        # In a normal install we re-invoke the interpreter with ``-m``. In a frozen (PyInstaller)
        # bundle there's no ``-m`` and ``sys.executable`` is the app itself, so we re-launch the
        # bundle with a ``--capture`` marker that packaging/entry.py routes to real_capture.main.
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--capture"]
        else:
            cmd = [sys.executable, "-m", "contextline.capture_script.real_capture"]
        if minutes and minutes > 0:
            cmd += ["--minutes", str(minutes)]
        if dry_run:
            cmd += ["--dry-run"]

        config.ensure_dirs()
        # Redirect the child's stdout/stderr to a log so it never blocks on a full pipe and the
        # user can see what was captured.
        self._capture_log = open(config.home() / "capture.log", "a")
        self._capture_proc = subprocess.Popen(cmd, stdout=self._capture_log, stderr=subprocess.STDOUT)
        return {
            "status": "started",
            "pid": self._capture_proc.pid,
            "minutes": minutes or None,
            "dry_run": dry_run,
            "note": "Screen capture is running in the background. Events land in pending/. "
                    "When done, call stop_capture, then process_today to build the journal.",
        }

    def stop_capture_process(self) -> dict:
        """Stop a running capture subprocess. Idempotent: returns ``not_running`` if idle.

        Sends SIGINT first so ``real_capture`` runs its clean-exit path (prints a summary);
        escalates to terminate/kill only if it doesn't stop promptly. Captured events are
        written incrementally, so a hard stop never loses already-recorded events.
        """
        proc = self._capture_proc
        if proc is None or proc.poll() is not None:
            self._capture_proc = None
            return {"status": "not_running"}

        pid = proc.pid
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._capture_proc = None
        if self._capture_log is not None:
            try:
                self._capture_log.close()
            except Exception:
                pass
            self._capture_log = None
        return {"status": "stopped", "pid": pid}

    def capture_status(self) -> dict:
        """Whether a background capture is currently running."""
        running = self._capture_proc is not None and self._capture_proc.poll() is None
        return {"running": running, "pid": self._capture_proc.pid if running else None}

    def build_llm_agent(self):
        """Return a fresh underlying ``LlmAgent`` (tools included) for an external runner.

        The FastAPI path uses ``chat()``, which drives this agent through the Root's own
        ``Runner``. ``adk web`` instead wants a module-level ``root_agent`` it can drive with
        its OWN runner/session service — this exposes exactly that same agent to it. See
        ``adk_web/contextline_agent/agent.py``.
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

        from contextline.observability import observe

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
