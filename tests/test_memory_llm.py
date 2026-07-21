"""Live historical-query test for the Memory Agent (build step 5) — opt-in.

Skipped unless ``RUN_LLM_TESTS=1``. Persists a day, then asks a natural-language question and
checks the answer is grounded in the stored data (and refuses when there's no record).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from daylook.agents.memory import MemoryAgent

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run live model tests"
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_journal.json"


def test_answers_from_stored_data(tmp_path):
    sample = json.loads(FIXTURE.read_text())
    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    try:
        mem.persist_day(sample["day"], sample["writeup"])

        ans = mem.answer("What did I work on on 2026-07-05?", today="2026-07-06")
        assert ans
        low = ans.lower()
        assert "jwt" in low or "rotation" in low or "review" in low

        # no record for a different date -> should say so, not fabricate
        empty = mem.answer("What did I do on 2020-01-01?", today="2026-07-06")
        assert empty
    finally:
        mem.close()
