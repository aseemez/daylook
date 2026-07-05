"""Live Root Agent test (build step 6) — opt-in.

Skipped unless ``RUN_LLM_TESTS=1``. Drives a real conversation through the Root: process the
day, ask about it, then set a mid-session refinement and confirm it's stored in session state
(the mechanism behind eval criterion 5). Uses a temp CONTEXTLINE_HOME so it's isolated.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run live model tests"
)


def test_root_conversation_and_refinement(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    monkeypatch.setenv("CONTEXTLINE_USE_MCP", "0")

    from contextline.capture_script.mock_generator import generate
    from contextline.agents.memory import MemoryAgent
    from contextline.agents.root import RootAgent

    generate(start=datetime(2026, 7, 5, 9, 0, 0), clean=True)  # into tmp pending

    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    root = RootAgent(memory=mem)

    async def convo():
        reply, sid = await root.chat("Process my day and show me the journal.")
        assert reply
        # the presented writeup should carry the deterministic timeline + numbers
        assert "Timeline" in reply or "–" in reply

        # set a mid-session refinement
        reply2, sid = await root.chat("Don't count Slack as focus time.", session_id=sid)
        assert reply2
        refinements = await root.get_refinements(sid)
        assert any("slack" in r.lower() for r in refinements), refinements

        # a history question in the same session
        reply3, sid = await root.chat("What did I work on on 2026-07-05?", session_id=sid)
        assert reply3
        return sid

    asyncio.run(convo())
    mem.close()
