"""Small helpers for driving one-shot ADK text agents.

The Root Agent manages its own stateful ADK session; this helper is for the simple
"prompt in, text out" agents (like the Journalist) that don't need conversation state.
"""

from __future__ import annotations

import asyncio
import os

from google.genai import types

# Ceiling on a single one-shot agent call. Without it a stalled connection (laptop asleep
# mid-request, provider outage) hangs the calling thread FOREVER — one background analyst
# call was observed wedged for 5.5 hours on 2026-07-23, holding the whole pipeline hostage.
# A timed-out call raises; the caller (chat turn or the hourly auto-journal) surfaces or
# retries on its own schedule, and untouched events stay safely in pending/.
CALL_TIMEOUT_SECONDS = int(os.environ.get("DAYLOOK_CALL_TIMEOUT_SECONDS", "240"))


async def _run_async(agent, message: str) -> str:
    from google.adk.runners import InMemoryRunner

    runner = InMemoryRunner(agent=agent, app_name=f"daylook-{agent.name}")
    session = await runner.session_service.create_session(
        app_name=f"daylook-{agent.name}", user_id=agent.name
    )
    content = types.Content(role="user", parts=[types.Part(text=message)])
    out = None
    async for ev in runner.run_async(
        user_id=agent.name, session_id=session.id, new_message=content
    ):
        if ev.is_final_response() and ev.content and ev.content.parts:
            out = ev.content.parts[0].text
    return out or ""


def run_text_agent(agent, message: str) -> str:
    """Run a text LlmAgent for a single turn and return its final text response.

    Bounded by :data:`CALL_TIMEOUT_SECONDS`; raises ``TimeoutError`` instead of hanging."""
    try:
        return asyncio.run(
            asyncio.wait_for(_run_async(agent, message), timeout=CALL_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"model call ({agent.name}) did not answer within {CALL_TIMEOUT_SECONDS}s"
        ) from None
