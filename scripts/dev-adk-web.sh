#!/usr/bin/env bash
# ADK dev UI against the DEV data home (never the real ~/.daylook).
set -euo pipefail
source "$(dirname "$0")/dev-env.sh"
cd "$(dirname "$0")/.."
exec adk web adk_web "$@"
