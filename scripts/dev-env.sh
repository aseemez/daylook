# Source this (or let the dev-*.sh wrappers do it) before any dev/test run.
#
# Why this exists: dev runs and the owner's REAL screen journal used to share
# ~/.contextline, so mock events and test chatter polluted the real data (see
# PROGRESS.md gotchas). Convention now:
#   - dev/test  -> <repo>/.dev_home   (this file; gitignored, disposable)
#   - real data -> ~/.contextline     (default when CONTEXTLINE_HOME is unset)
#   - eval      -> <repo>/.eval_home  (set by eval.py itself)
#
# NOTE: never export GOOGLE_GEMINI_BASE_URL here — it would hijack the Vertex
# dev flow (google-genai honors it even when Vertex is configured). That var
# belongs only to the packaged app / relay tests.

_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
export CONTEXTLINE_HOME="$_REPO_ROOT/.dev_home"
export CONTEXTLINE_USE_MCP=0
unset _REPO_ROOT

echo "[dev-env] CONTEXTLINE_HOME=$CONTEXTLINE_HOME" >&2
