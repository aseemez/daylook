#!/usr/bin/env bash
# Build Contextline.app with PyInstaller in a dedicated Python 3.13 env.
#
# Why a separate env: PyInstaller against Python 3.14 is unproven here, and we pin the exact
# google-genai / google-adk versions the pipeline was validated on so a silent upstream change
# can't break the frozen bundle. The dev .venv (3.14) is left untouched.
#
#   packaging/build.sh            # full build + smoke test
#
# Output: dist/Contextline.app
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BUILD_VENV="packaging/.build-venv"
PY_VERSION="3.13.7"

echo "==> Creating build venv ($PY_VERSION) at $BUILD_VENV"
uv venv --python "$PY_VERSION" "$BUILD_VENV"
PY="$BUILD_VENV/bin/python"

echo "==> Installing pinned deps + project (with capture extras) + PyInstaller"
uv pip install --python "$PY" \
    "google-adk==2.3.0" \
    "google-genai==2.10.0" \
    "pyinstaller>=6.6" \
    ".[capture]"

echo "==> Cleaning previous build artifacts"
rm -rf build/pyi dist/Contextline dist/Contextline.app

echo "==> Running PyInstaller"
"$BUILD_VENV/bin/pyinstaller" packaging/contextline.spec \
    --noconfirm --distpath dist --workpath build/pyi

APP="dist/Contextline.app"
BIN="$APP/Contents/MacOS/Contextline"
if [[ ! -x "$BIN" ]]; then
    echo "!! Build failed: $BIN not found" >&2
    exit 1
fi

echo "==> Smoke-testing the frozen app (boots server, checks /health)"
# Point at a throwaway home so the smoke run never touches the real journal.
SMOKE_HOME="$(mktemp -d)"
if CONTEXTLINE_HOME="$SMOKE_HOME" "$BIN" --smoke --no-browser; then
    echo "==> Smoke test PASSED"
else
    echo "!! Smoke test FAILED — the bundle boots but /health did not answer" >&2
    rm -rf "$SMOKE_HOME"
    exit 1
fi
rm -rf "$SMOKE_HOME"

echo "==> Done: $APP"
du -sh "$APP" | awk '{print "    bundle size: " $1}'
