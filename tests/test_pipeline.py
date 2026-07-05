"""Hermetic test of the processing pipeline (build step 6 support).

Injects stub analyze/journal functions so the whole orchestration — capture, aggregate,
journal, persist, move-to-processed, and refinement threading — is exercised without any
model calls.
"""

from __future__ import annotations

from datetime import datetime

from contextline.agents.analyst import BatchAnalysis, LabeledBlock
from contextline.agents.memory import MemoryAgent
from contextline.capture_script.mock_generator import generate
from contextline.pipeline import Pipeline


def _fake_analyze(batches, pending_dir=None, use_cache=True):
    analyses = []
    for b in batches:
        block = LabeledBlock(
            start=b.events[0].timestamp,
            end=b.events[-1].timestamp,
            duration_seconds=600,
            label=f"Work in {b.events[0].app_name}",
            category="coding",
            app_name=b.events[0].app_name,
            source_event_ids=list(b.event_ids),
        )
        analyses.append(BatchAnalysis(batch_id=b.batch_id, date=b.date, blocks=[block]))
    return analyses


def test_pipeline_processes_and_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))  # pending + processed under tmp
    from contextline import config
    pending = config.pending_dir()
    generate(pending=pending, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)

    captured_refinements = {}

    def fake_journal(day, refinements=None, use_cache=True):
        captured_refinements["value"] = refinements
        return f"# Journal — {day['date']}\n\n{len(day['blocks'])} blocks."

    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=fake_journal)

    result = pipe.process_day(refinements=["exclude Slack from focus time"])

    # produced a dated result with blocks
    assert result["date"] == "2026-07-05"
    assert result["num_events"] == 30
    assert result["num_blocks"] >= 3
    assert result["writeup"].startswith("# Journal — 2026-07-05")

    # refinements were threaded into the journalist
    assert captured_refinements["value"] == ["exclude Slack from focus time"]

    # persisted to long-term memory (eval criterion 3 shape)
    row = mem.get_journal("2026-07-05")
    assert row is not None and row["writeup"] == result["writeup"]

    # events moved out of pending into processed
    assert not list(pending.glob("*.json"))
    assert list(config.processed_dir().glob("*.json"))
    mem.close()


def test_pipeline_handles_empty_pending(tmp_path):
    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=lambda *a, **k: "x")
    result = pipe.process_day(pending_dir=tmp_path / "empty")
    assert result["num_blocks"] == 0
    assert "No pending" in result["writeup"]
    mem.close()
