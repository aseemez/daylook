"""Live narrative test for the Journalist (build step 4) — opt-in.

Skipped unless ``RUN_LLM_TESTS=1``. Verifies the real model produces a non-empty recap and
that the composed writeup carries it alongside the deterministic timeline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from contextline.agents import journalist

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run live model tests"
)

FIXTURE = Path(__file__).parent / "fixtures" / "labeled_blocks.json"


def test_live_narrative_and_full_writeup(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))  # isolate cache
    day = json.loads(FIXTURE.read_text())

    narrative = journalist.write_narrative(day)
    assert narrative and len(narrative) > 40

    out = journalist.write_journal(day)
    assert narrative.split("\n")[0][:20] in out       # narrative embedded
    assert "## Timeline" in out and "## By the numbers" in out

    # second call served from cache (identical, no error)
    assert journalist.write_narrative(day) == narrative
