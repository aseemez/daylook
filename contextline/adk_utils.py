"""Small helpers for driving one-shot ADK text agents.

The Root Agent manages its own stateful ADK session; this helper is for the simple
"prompt in, text out" agents (like the Journalist) that don't need conversation state.
"""

from __future__ import annotations

import asyncio

from google.genai import types


async def _run_async(agent, message: str) -> str:
    from google.adk.runners import InMemoryRunner

    runner = InMemoryRunner(agent=agent, app_name=f"contextline-{agent.name}")
    session = await runner.session_service.create_session(
        app_name=f"contextline-{agent.name}", user_id=agent.name
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
    """Run a text LlmAgent for a single turn and return its final text response."""
    return asyncio.run(_run_async(agent, message))
