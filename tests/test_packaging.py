"""Tests for the packaged-app seams that can't be caught by importing normally.

The frozen bundle's ``entry.py`` still exposes a ``--capture`` CLI role (handy for manual/CLI
capture), even though the running app now captures in-process on a thread rather than
re-launching itself. These lock in that entry routing and the macOS argv cleanup.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys


def _load_entry():
    # Load packaging/entry.py by path — importing it as `packaging.entry` would collide with the
    # installed PyPI `packaging` library, which shadows our top-level packaging/ directory.
    path = pathlib.Path(__file__).resolve().parent.parent / "packaging" / "entry.py"
    spec = importlib.util.spec_from_file_location("cl_frozen_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_routes_capture_flag_to_real_capture(monkeypatch):
    """`Daylook --capture ...` runs the capture CLI, forwarding the remaining args."""
    entry = _load_entry()
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr("daylook.capture_script.real_capture.main", fake_main)
    monkeypatch.setattr(sys, "argv", ["Daylook", "--capture", "--dry-run"])
    rc = entry.main()
    assert rc == 0
    assert "--dry-run" in seen["argv"]
    assert "--capture" not in seen["argv"]  # the marker is consumed, not forwarded


def test_entry_clean_argv_drops_process_serial_number():
    entry = _load_entry()
    assert entry._clean_argv(["--smoke", "-psn_0_12345", "--no-browser"]) == ["--smoke", "--no-browser"]
