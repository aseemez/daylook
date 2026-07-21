"""ADK dev-UI entry point for Daylook.

``adk web`` discovers agents by importing a package that exposes a module-level
``root_agent``. Daylook's real orchestrator is the :class:`RootAgent` class in
``daylook.agents.root`` — it wires the capture -> analyst -> journalist -> memory
Pipeline and a SQLite-backed MemoryAgent behind four tools, and for the FastAPI path it
runs them through its own ADK ``Runner``. Here we reuse that *same* underlying
``LlmAgent`` (tools included) and expose it as ``root_agent`` so the ADK dev UI can drive
it with ITS own runner and session service.

Because ``adk web`` supplies the session state, the session-scoped refinements
(e.g. "exclude Slack from focus time") work through ``tool_context.state`` exactly as in
the FastAPI path — no extra wiring needed. Vertex routing env vars are loaded from the
repo-root ``.env`` on import (via ``daylook.config``), so run ``adk web`` from the
repo root.

Run:

    adk web adk_web        # from the repo root, then pick "daylook_agent" in the UI

Try in the UI: "process today" (needs events in ~/.daylook/pending — seed with the
mock generator or the real capture script first), then "what did I do today?", then a
refinement like "don't count Slack as focus time".
"""

from daylook.agents.root import RootAgent

# Constructing RootAgent wires a MemoryAgent (SQLite) + the full Pipeline behind the tool
# closures; we then hand adk web the underlying LlmAgent. (RootAgent also builds an
# InMemoryRunner for its own FastAPI path — unused here and harmless.) adk web provides the
# Runner + per-session state that the tools read via tool_context.
root_agent = RootAgent().build_llm_agent()
