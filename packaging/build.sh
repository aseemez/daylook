#!/usr/bin/env bash
# Build Daylook.app with PyInstaller in a dedicated Python 3.13 env.
#
# Why a separate env: PyInstaller against Python 3.14 is unproven here, and we pin the exact
# google-genai / google-adk versions the pipeline was validated on so a silent upstream change
# can't break the frozen bundle. The dev .venv (3.14) is left untouched.
#
#   packaging/build.sh            # full build + smoke test
#
# Output: dist/Daylook.app
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BUILD_VENV="packaging/.build-venv"
PY_VERSION="3.13.7"

echo "==> Ensuring build venv ($PY_VERSION) at $BUILD_VENV"
# Reuse an existing venv (fast repeat builds); create it only if missing.
if [[ ! -d "$BUILD_VENV" ]]; then
    uv venv --python "$PY_VERSION" "$BUILD_VENV"
fi
PY="$BUILD_VENV/bin/python"

echo "==> Installing pinned deps + project (with capture extras) + PyInstaller"
# --reinstall-package daylook is load-bearing: the project version rarely changes, so without
# it uv would consider the already-installed copy "satisfied" and skip picking up local source
# edits (e.g. the baked-in relay URL) — silently shipping a stale bundle.
uv pip install --python "$PY" --reinstall-package daylook \
    "google-adk==2.3.0" \
    "google-genai==2.10.0" \
    "pyinstaller>=6.6" \
    ".[capture]"

echo "==> Cleaning previous build artifacts"
rm -rf build/pyi dist/Daylook dist/Daylook.app

echo "==> Running PyInstaller"
"$BUILD_VENV/bin/pyinstaller" packaging/daylook.spec \
    --noconfirm --distpath dist --workpath build/pyi

APP="dist/Daylook.app"
BIN="$APP/Contents/MacOS/Daylook"
if [[ ! -x "$BIN" ]]; then
    echo "!! Build failed: $BIN not found" >&2
    exit 1
fi

echo "==> Code-signing the bundle"
# The signing identity decides whether macOS permissions SURVIVE A REBUILD. TCC (Screen
# Recording etc.) keys off the app's designated requirement:
#   * ad-hoc ("-"): the requirement IS the binary's hash, so every rebuild is a brand-new
#     identity and macOS silently revokes the grant — the app then captures only the desktop
#     wallpaper while believing it works (this cost two days of journals). Grants only stick
#     across LAUNCHES of one unmodified build.
#   * a real certificate (even a free "Apple Development" one from Xcode): the requirement
#     anchors to the cert + bundle id, so rebuilds keep the same identity and the one-time
#     grant sticks across updates too.
# CODESIGN_IDENTITY overrides; otherwise auto-pick the first Apple Development/Developer ID
# cert in the keychain, falling back to ad-hoc (with a loud warning) if none exists. Neither
# removes the first-open Gatekeeper prompt (that needs a paid Developer ID + notarization).
IDENTITY="${CODESIGN_IDENTITY:-}"
if [[ -z "$IDENTITY" ]]; then
    IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
        | grep -oE '"(Developer ID Application|Apple Development)[^"]*"' | head -1 | tr -d '"')" || true
fi
if [[ -n "$IDENTITY" ]]; then
    echo "    signing with: $IDENTITY"
    codesign --force --deep --sign "$IDENTITY" "$APP"
else
    echo "    WARNING: no signing certificate found — falling back to ad-hoc."
    echo "    Screen Recording permission will be REVOKED every time this app is rebuilt."
    codesign --force --deep --sign - "$APP"
fi
codesign --verify --deep --strict "$APP" && echo "    signature OK ($(codesign -dv "$APP" 2>&1 | grep -oE 'Signature=adhoc|TeamIdentifier=[A-Z0-9]+' | head -1))"
# Strip quarantine locally so our own smoke run doesn't get App-Translocated to a random path
# (which would itself defeat TCC persistence during testing).
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo "==> Smoke-testing the frozen app (boots server, checks /health)"
# Point at a throwaway home so the smoke run never touches the real journal.
SMOKE_HOME="$(mktemp -d)"
if DAYLOOK_HOME="$SMOKE_HOME" "$BIN" --smoke --no-browser; then
    echo "==> Smoke test PASSED"
else
    echo "!! Smoke test FAILED — the bundle boots but /health did not answer" >&2
    rm -rf "$SMOKE_HOME"
    exit 1
fi
rm -rf "$SMOKE_HOME"

echo "==> Done: $APP"
du -sh "$APP" | awk '{print "    bundle size: " $1}'
