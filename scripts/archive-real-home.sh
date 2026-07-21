#!/usr/bin/env bash
# One-time cleanup of a ~/.daylook that mixed dev/test data with real
# captures. MOVES (never deletes) journal data into a dated archive subfolder
# so the real home starts fresh. cache/ is kept in place: it's content-
# addressed (keyed on input bytes), so stale entries are harmless and reusing
# them saves API money.
set -euo pipefail

HOME_DIR="${DAYLOOK_HOME:-$HOME/.daylook}"
ARCHIVE="$HOME_DIR/archive-$(date +%Y%m%d-%H%M%S)"

if [ ! -d "$HOME_DIR" ]; then
  echo "Nothing to archive: $HOME_DIR does not exist."
  exit 0
fi

mkdir -p "$ARCHIVE"
moved=0
for item in pending processed journal.sqlite daylook.log capture.log; do
  src="$HOME_DIR/$item"
  if [ -e "$src" ]; then
    mv "$src" "$ARCHIVE/$item"
    echo "moved  $src -> $ARCHIVE/$item"
    moved=1
  fi
done

if [ "$moved" -eq 0 ]; then
  rmdir "$ARCHIVE"
  echo "Nothing to archive in $HOME_DIR (already clean)."
else
  echo
  echo "Done. Old data preserved under: $ARCHIVE"
  echo "Kept in place: $HOME_DIR/cache (content-addressed, saves API cost)."
fi
