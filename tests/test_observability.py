"""Tests for structured logging (build step 7).

Runs the pipeline (with stubbed models) and asserts a readable structured record was written
for each stage, with latency and input/output summaries.
"""

from __future__ import annotations

from datetime import datetime

from contextline import observability
from contextline.agents.analyst import BatchAnalysis, LabeledBlock
from contextline.agents.memory import MemoryAgent
from contextline.capture_script.mock_generator import generate
from contextline.pipeline import Pipeline


def _fake_analyze(batches, pending_dir=None, use_cache=True):
    out = []
    for b in batches:
        out.append(BatchAnalysis(batch_id=b.batch_id, date=b.date, blocks=[
            LabeledBlock(
                start=b.events[0].timestamp, end=b.events[-1].timestamp,
                duration_seconds=600, label="Work", category="coding",
                app_name=b.events[0].app_name, source_event_ids=list(b.event_ids),
            )
        ]))
    return out


def test_pipeline_emits_structured_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    from contextline import config
    generate(pending=config.pending_dir(), start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    observability.clear()

    mem = MemoryAgent(db_path=tmp_path / "journal.sqlite")
    pipe = Pipeline(mem, analyze_fn=_fake_analyze, journal_fn=lambda *a, **k: "journal text")
    pipe.process_day()
    mem.close()

    events = observability.read_events()
    agents = {e["agent"] for e in events}
    assert {"capture", "analyst", "journalist", "memory"} <= agents

    for e in events:
        assert "latency_ms" in e and e["latency_ms"] >= 0
        assert "action" in e and "ts" in e
        assert "output" in e


def test_read_events_empty_when_no_log(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    assert observability.read_events() == []
