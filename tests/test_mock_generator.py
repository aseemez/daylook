"""Tests for the mock capture generator (build step 1).

These pin the on-disk contract the whole pipeline depends on: for every event there is a
matching JSON sidecar and JPEG, the schema is exactly right, and timestamps march forward.
"""

from __future__ import annotations

import json
from datetime import datetime

from PIL import Image

from daylook.capture_script.mock_generator import SCENARIO, generate
from daylook.events import CaptureEvent, load_events


def test_generate_writes_matching_pairs(tmp_path):
    events = generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)

    assert len(events) == len(SCENARIO)
    jsons = sorted(tmp_path.glob("*.json"))
    jpgs = sorted(tmp_path.glob("*.jpg"))
    assert len(jsons) == len(events)
    assert len(jpgs) == len(events)

    # every sidecar has a same-stem image
    for j in jsons:
        assert j.with_suffix(".jpg").exists()


def test_sidecar_schema_is_exact(tmp_path):
    generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    sample = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert set(sample.keys()) == {"event_id", "timestamp", "app_name", "window_title", "kind"}
    # round-trips through the shared schema
    CaptureEvent.from_dict(sample)


def test_timestamps_are_monotonic(tmp_path):
    events = generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    loaded = load_events(tmp_path)
    times = [e.dt for e in loaded]
    assert times == sorted(times)
    assert len(loaded) == len(events)


def test_images_are_valid_and_within_bounds(tmp_path):
    generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    for jpg in tmp_path.glob("*.jpg"):
        with Image.open(jpg) as im:
            im.verify()
        with Image.open(jpg) as im:
            assert max(im.size) <= 1024  # within IMAGE_MAX_EDGE


def test_clean_wipes_previous_events(tmp_path):
    generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    first_count = len(list(tmp_path.glob("*.json")))
    # regenerate with clean — count stays the same, not doubled
    generate(pending=tmp_path, start=datetime(2026, 7, 5, 9, 0, 0), clean=True)
    assert len(list(tmp_path.glob("*.json"))) == first_count
