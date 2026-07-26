"""Register/unregister Daylook as a macOS login item (Phase 1 auto-start).

Two mechanisms, tried in order, both best-effort (this must never crash the app or a settings
write — a failed registration just means the user opens the app manually):

  1. **SMAppService** (macOS 13+, the modern API): registers the *main app bundle* as a login
     item that appears in System Settings → General → Login Items. Only meaningful when we're
     actually running as the packaged .app (``sys.frozen``) — in dev the "main app" is the
     Python interpreter, which we must never auto-launch.
  2. **LaunchAgent plist** fallback: writes ``~/Library/LaunchAgents/com.daylook.app.plist``
     pointing at the app binary with ``RunAtLoad``. Works on any macOS and needs no signing,
     so it's the reliable path when SMAppService is unavailable or refuses.

``set_login_item(enabled)`` returns True on success, False on a handled failure, None when the
operation isn't applicable (e.g. asked to enable from a dev run where there's no app bundle).
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

LAUNCH_AGENT_LABEL = "com.daylook.app"
_LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _try_smappservice(enable: bool) -> bool | None:
    """Register/unregister via SMAppService. None = not available/applicable."""
    if not _frozen():
        return None  # dev run — never register the interpreter as a login item
    try:
        from ServiceManagement import SMAppService  # type: ignore
    except Exception:
        return None
    try:
        svc = SMAppService.mainAppService()
        # pyobjc surfaces the ObjC ``error:`` out-param as a second return value.
        if enable:
            result = svc.registerAndReturnError_(None)
        else:
            result = svc.unregisterAndReturnError_(None)
        ok = result[0] if isinstance(result, tuple) else result
        return bool(ok)
    except Exception:
        return False


def _app_binary() -> str | None:
    """Path to the launchable binary for a LaunchAgent (frozen app only)."""
    if not _frozen():
        return None
    return sys.executable  # …/Daylook.app/Contents/MacOS/Daylook


def _try_launch_agent(enable: bool) -> bool | None:
    """Write/remove the LaunchAgent plist. None = can't (no app binary to point at)."""
    try:
        if enable:
            binary = _app_binary()
            if not binary:
                return None
            _LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "Label": LAUNCH_AGENT_LABEL,
                # --no-browser: at login we want it recording quietly in the menubar, not
                # popping a browser tab. The user opens the journal from the menubar when they
                # want it.
                "ProgramArguments": [binary, "--no-browser"],
                "RunAtLoad": True,
                "ProcessType": "Interactive",
            }
            _LAUNCH_AGENT_PATH.write_bytes(plistlib.dumps(payload))
            return True
        else:
            if _LAUNCH_AGENT_PATH.exists():
                _LAUNCH_AGENT_PATH.unlink()
            return True
    except OSError:
        return False


def set_login_item(enabled: bool) -> bool | None:
    """Enable/disable launch-at-login. Prefers SMAppService, falls back to a LaunchAgent."""
    via_sm = _try_smappservice(enabled)
    if via_sm is True:
        # Keep the two mechanisms from fighting: if the modern API took it, drop any stale
        # LaunchAgent we may have written on an earlier macOS/build.
        if not enabled:
            _try_launch_agent(False)
        return True
    return _try_launch_agent(enabled)


def is_enabled() -> bool:
    """Best-effort read of whether launch-at-login looks active (the LaunchAgent plist, which
    SMAppService doesn't expose readably). Used only as a UI hint; settings.json is the truth."""
    return _LAUNCH_AGENT_PATH.exists()
