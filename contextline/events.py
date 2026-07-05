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


@dataclass(frozen=True)
class EventBatch:
    """A contiguous run of capture events handed to the Analyst as one unit.

    One batch == one Analyst (vision) call. The Capture Agent produces these; the Analyst
    turns each into labeled semantic blocks.
    """

    batch_id: str
    events: tuple[CaptureEvent, ...]

    @property
    def start(self) -> datetime:
        return self.events[0].dt

    @property
    def end(self) -> datetime:
        return self.events[-1].dt

    @property
    def event_ids(self) -> list[str]:
        return [e.event_id for e in self.events]

    @property
    def date(self) -> str:
        """The calendar date (YYYY-MM-DD) this batch belongs to, from its first event."""
        return self.events[0].dt.date().isoformat()

    def __len__(self) -> int:
        return len(self.events)
