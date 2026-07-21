# Source this (or let the dev-*.sh wrappers do it) before any dev/test run.
#
# Why this exists: dev runs and the owner's REAL screen journal used to share
# ~/.daylook, so mock events and test chatter polluted the real data (see
# PROGRESS.md gotchas). Convention now:
#   - dev/test  -> <repo>/.dev_home   (this file; gitignored, disposable)
#   - real data -> ~/.daylook     (default when DAYLOOK_HOME is unset)
#   - eval      -> <repo>/.eval_home  (set by eval.py itself)
#
# NOTE: never export GOOGLE_GEMINI_BASE_URL here — google-genai honors it
# unconditionally, so it would silently reroute dev model calls (which use the
# free-tier GOOGLE_API_KEY from .env). That var belongs only to the packaged
# app / relay tests.

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export DAYLOOK_HOME="$_REPO_ROOT/.dev_home"
export DAYLOOK_USE_MCP=0
unset _REPO_ROOT

echo "[dev-env] DAYLOOK_HOME=$DAYLOOK_HOME" >&2
