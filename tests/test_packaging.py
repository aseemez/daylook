"""Tests for the packaged-app seams that can't be caught by importing normally.

These lock in the two frozen-app behaviors that have no effect in a normal install (so a refactor
can't silently break the .app): the ``--capture`` re-launch command and the argv cleanup.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

from contextline.agents.root import RootAgent


def _load_entry():
    # Load packaging/entry.py by path — importing it as `packaging.entry` would collide with the
    # installed PyPI `packaging` library, which shadows our top-level packaging/ directory.
    path = pathlib.Path(__file__).resolve().parent.parent / "packaging" / "entry.py"
    spec = importlib.util.spec_from_file_location("cl_frozen_entry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_start_capture_uses_module_form_when_not_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    monkeypatch.delattr(sys, "frozen", raising=False)
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    RootAgent().start_capture_process(dry_run=True)
    assert captured["cmd"][1:3] == ["-m", "contextline.capture_script.real_capture"]
    assert "--dry-run" in captured["cmd"]


def test_start_capture_uses_capture_flag_when_frozen(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTLINE_HOME", str(tmp_path))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            self.pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    RootAgent().start_capture_process(minutes=5, dry_run=True)
    # frozen: re-launch the bundle itself with the --capture marker, no `-m module`
    assert captured["cmd"][0] == sys.executable
    assert captured["cmd"][1] == "--capture"
    assert "-m" not in captured["cmd"]
    assert "--minutes" in captured["cmd"] and "5" in captured["cmd"]


def test_entry_clean_argv_drops_process_serial_number():
    entry = _load_entry()
    assert entry._clean_argv(["--smoke", "-psn_0_12345", "--no-browser"]) == ["--smoke", "--no-browser"]
