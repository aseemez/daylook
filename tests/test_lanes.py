"""Productivity lanes: the roll-up from granular category to a focus/social/idle lane.

The calendar colours by lane and the assistant scores focus vs. distraction from it, so the
mapping (config.LANES) and its productive/distracting/neutral verdict need to stay stable.
"""

from __future__ import annotations

from daylook import config


def test_lane_mapping_covers_all_categories():
    from daylook.agents.analyst import CATEGORIES
    for cat in CATEGORIES:
        assert cat in config.LANES, f"category {cat} has no lane"


def test_lane_and_productivity_verdicts():
    assert config.lane_of("coding") == "focus"
    assert config.lane_of("research") == "research"
    assert config.lane_of("communication") == "comms"
    assert config.lane_of("social") == "social"
    assert config.lane_of("entertainment") == "social"
    assert config.lane_of("break") == "idle"
    assert config.lane_of(None) == "idle"            # unknown -> idle
    assert config.lane_of("nonsense") == "idle"

    assert config.productivity_of("coding") == "productive"
    assert config.productivity_of("research") == "productive"
    assert config.productivity_of("communication") == "productive"
    assert config.productivity_of("social") == "distracting"
    assert config.productivity_of("entertainment") == "distracting"
    assert config.productivity_of("break") == "neutral"
