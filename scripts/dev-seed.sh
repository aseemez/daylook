#!/usr/bin/env bash
# Seed mock capture events into the DEV data home's pending/ folder.
set -euo pipefail
source "$(dirname "$0")/dev-env.sh"
cd "$(dirname "$0")/.."
exec python -m contextline.capture_script.mock_generator "$@"
