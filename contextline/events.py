"""The capture-event schema — shared by the (mock) capture layer and the Capture Agent.

A capture event is two files that share a stem:
  - ``<event_id>.json``  the sidecar: timestamp, app_name, window_title, event_id
  - ``<event_id>.jpg``   a downscaled screenshot of the frontmost window at that moment

Keeping the schema in one place means the generator and the reader can never drift.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class CaptureEvent:
    event_id: str
    timestamp: str        # ISO 8601, e.g. "2026-07-05T09:15:00"
    app_name: str
    window_title: str

    # --- serialization ---

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CaptureEvent":
        return cls(
            event_id=d["event_id"],
            timestamp=d["timestamp"],
            app_name=d["app_name"],
            window_title=d["window_title"],
        )

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.timestamp)

    # --- filesystem layout ---

    def sidecar_path(self, folder: Path) -> Path:
        return Path(folder) / f"{self.event_id}.json"

    def image_path(self, folder: Path) -> Path:
        return Path(folder) / f"{self.event_id}.jpg"

    def write_sidecar(self, folder: Path) -> Path:
        path = self.sidecar_path(folder)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


def load_event(sidecar: Path) -> CaptureEvent:
    """Load a single event from its JSON sidecar path."""
    return CaptureEvent.from_dict(json.loads(Path(sidecar).read_text()))


def load_events(folder: Path) -> list[CaptureEvent]:
    """Load all events in a folder, sorted by timestamp then event_id (stable order)."""
    folder = Path(folder)
    events = [load_event(p) for p in folder.glob("*.json")]
    return sorted(events, key=lambda e: (e.timestamp, e.event_id))
