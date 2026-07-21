#!/usr/bin/env bash
# FastAPI server against the DEV data home (never the real ~/.daylook).
set -euo pipefail
source "$(dirname "$0")/dev-env.sh"
cd "$(dirname "$0")/.."
exec uvicorn daylook.api.main:app --reload "$@"
