#!/usr/bin/env bash
# FastAPI server against the DEV data home (never the real ~/.contextline).
set -euo pipefail
source "$(dirname "$0")/dev-env.sh"
cd "$(dirname "$0")/.."
exec uvicorn contextline.api.main:app --reload "$@"
