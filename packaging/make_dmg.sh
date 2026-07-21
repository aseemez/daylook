#!/usr/bin/env bash
# Package dist/Daylook.app into a drag-to-install dist/Daylook.dmg.
#
# Run after packaging/build.sh:
#   packaging/make_dmg.sh
#
# The dmg mounts to a window showing Daylook.app next to an /Applications alias, so the user
# just drags across. It is UNSIGNED — on first open the user takes the Sequoia
# Settings -> Privacy & Security -> "Open Anyway" path (documented on the landing page). Signing
# + notarization is the $99 "when there's money" step.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

APP="dist/Daylook.app"
DMG="dist/Daylook.dmg"
VOL="Daylook"

[[ -d "$APP" ]] || { echo "!! $APP not found — run packaging/build.sh first" >&2; exit 1; }

STAGE="$(mktemp -d)"
echo "==> Staging bundle for the dmg"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG"
echo "==> Building $DMG"
hdiutil create -volname "$VOL" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null

rm -rf "$STAGE"
echo "==> Done: $DMG"
du -sh "$DMG" | awk '{print "    dmg size: " $1}'
