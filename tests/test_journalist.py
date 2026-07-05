"""Tests for the Journalist (build step 4).

Hermetic: the LLM narrative is stubbed, so these run offline and free. They verify the
deterministic timeline + stats rendering and the eval-shaped invariants (time-bucketed
entries present; a real distribution present).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from contextline.agents import journalist

FIXTURE = Path(__file__).parent / "fixtures" / "labeled_blocks.json"


@pytest.fixture
def day():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(autouse=True)
def _stub_narrative(monkeypatch):
    monkeypatch.setattr(journalist, "write_narrative", lambda *a, **k: "A productive day.")


def test_writeup_has_time_bucketed_timeline(day):
    out = journalist.write_journal(day)
    assert "## Timeline" in out
    # at least one HH:MM–HH:MM bucket (eval criterion 1: buckets, not just prose)
    assert re.search(r"\d{2}:\d{2}–\d{2}:\d{2} — ", out)


def test_writeup_has_distribution(day):
    out = journalist.write_journal(day)
    assert "## By the numbers" in out
    assert "By category:" in out
    assert "Top apps:" in out


def test_writeup_includes_real_labels(day):
    out = journalist.write_journal(day)
    # a label from the fixture should appear verbatim in the timeline
    some_label = day["blocks"][0]["label"]
    assert some_label in out


def test_timeline_durations_are_minutes(day):
    out = journalist.write_journal(day)
    # every timeline line ends with "(App, Nm)"
    lines = [l for l in out.splitlines() if l.startswith("- ") and "—" in l]
    assert lines
    for l in lines:
        assert re.search(r", \d+m\)$", l), l


def test_empty_day_is_handled():
    out = journalist.write_journal({"date": "2026-07-05", "blocks": []})
    assert "No tracked activity" in out


def test_stats_reflect_real_totals(day):
    out = journalist.write_journal(day)
    total_min = day["total_tracked_seconds"] // 60
    assert f"Total tracked: {total_min}m" in out
