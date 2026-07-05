"""Evaluation harness (build step 8).

Runs the real pipeline against a fixed, deterministic fixture and scores five concrete
criteria (CLAUDE.md). Each is a boolean; the script prints per-criterion pass/fail and an
overall score.

Cost: the first run makes a handful of model calls; **reruns are free** because every model
call is served from the on-disk cache. The harness uses a stable eval home
(``.eval_home/``) so that cache — and the SQLite db — persist across runs. If a rerun makes
real API calls, that's a bug.

    python eval.py
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

# Pin storage to a stable, repo-local home BEFORE importing anything that reads config, so the
# response cache and journal db persist across runs (making reruns free) and never touch the
# user's real ~/.contextline data. Force the plain filesystem read path for determinism.
EVAL_HOME = Path(__file__).parent / ".eval_home"
os.environ["CONTEXTLINE_HOME"] = str(EVAL_HOME)
os.environ.setdefault("CONTEXTLINE_USE_MCP", "0")

from contextline import config, observability          # noqa: E402
from contextline.agents.journalist import write_narrative  # noqa: E402
from contextline.agents.memory import MemoryAgent      # noqa: E402
from contextline.capture_script.mock_generator import generate  # noqa: E402
from contextline.events import load_events             # noqa: E402
from contextline.pipeline import Pipeline              # noqa: E402

FIXTURE_START = datetime(2026, 7, 5, 9, 0, 0)
FIXTURE_DATE = "2026-07-05"

TIME_BUCKET_RE = re.compile(r"\d{2}:\d{2}[–-]\d{2}:\d{2}")


def run_eval(verbose: bool = True) -> dict:
    config.ensure_dirs()
    observability.clear()

    # Deterministic fixture into pending. id->app map lets us cross-check for hallucination.
    events = generate(pending=config.pending_dir(), start=FIXTURE_START, clean=True)
    id_to_app = {e.event_id: e.app_name for e in events}
    valid_ids = set(id_to_app)

    mem = MemoryAgent(db_path=config.sqlite_path())
    pipe = Pipeline(mem)
    result = pipe.process_day(move_processed=False)  # keep fixture reusable across runs
    day = result["day"]
    writeup = result["writeup"]
    blocks = day.get("blocks", [])

    criteria: dict[str, bool] = {}
    detail: dict[str, str] = {}

    # 1) Time-bucketed blocks, not just prose.
    buckets = TIME_BUCKET_RE.findall(writeup)
    criteria["1_time_bucketed"] = len(buckets) >= 3
    detail["1_time_bucketed"] = f"{len(buckets)} time-bucket entries in writeup"

    # 2) No hallucinated app names — every block's app_name is an app of its source events,
    #    and every source_event_id is real.
    ok2 = True
    bad = []
    for b in blocks:
        src = b.get("source_event_ids", [])
        if not set(src) <= valid_ids:
            ok2 = False
            bad.append(f"{b['label']}: unknown event ids")
            continue
        source_apps = {id_to_app[e] for e in src}
        if b["app_name"] not in source_apps:
            ok2 = False
            bad.append(f"{b['label']}: app '{b['app_name']}' not in source events {source_apps}")
    criteria["2_no_hallucinated_apps"] = ok2 and bool(blocks)
    detail["2_no_hallucinated_apps"] = "all blocks trace to real events/apps" if ok2 else "; ".join(bad)

    # 3) Memory persistence — SQLite row exists and matches the writeup.
    row = mem.get_journal(FIXTURE_DATE)
    criteria["3_memory_persisted"] = bool(row) and row["writeup"] == writeup
    detail["3_memory_persisted"] = "row present and writeup matches" if criteria["3_memory_persisted"] else "missing/mismatched row"

    # 4) Analytical content — at least one time distribution or ranking.
    has_dist = bool(day.get("time_by_category")) or bool(day.get("app_ranking"))
    criteria["4_analytical_content"] = has_dist and ("By the numbers" in writeup)
    detail["4_analytical_content"] = f"categories={list(day.get('time_by_category', {}).keys())}"

    # 5) Session refinement respected — an exclusion changes subsequent output. The baseline
    #    recap centers on the day's dominant thread (the JWT / auth work); under an explicit
    #    exclusion refinement that thread must disappear. Both calls are cached, so reruns are
    #    free. (Baseline shares the pipeline's journalist cache key, so it costs nothing here.)
    def _mentions_jwt(text: str) -> bool:
        t = text.lower()
        return "jwt" in t or "jwks" in t or "token rotation" in t

    baseline = write_narrative(day)
    refined = write_narrative(day, refinements=[
        "Do not mention JWT, JWKS, tokens, token rotation, or authentication work anywhere. "
        "Omit that thread of work entirely from the recap."
    ])
    base_has = _mentions_jwt(baseline)
    refined_has = _mentions_jwt(refined)
    criteria["5_refinement_respected"] = base_has and not refined_has
    detail["5_refinement_respected"] = f"baseline mentions JWT-work={base_has}, refined mentions JWT-work={refined_has}"

    mem.close()

    passed = sum(criteria.values())
    total = len(criteria)
    summary = {"criteria": criteria, "detail": detail, "score": f"{passed}/{total}", "passed": passed, "total": total}

    if verbose:
        _print_report(summary)
    return summary


def _print_report(summary: dict) -> None:
    print("\nContextline evaluation")
    print("=" * 60)
    for name, ok in summary["criteria"].items():
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}")
        print(f"         {summary['detail'][name]}")
    print("=" * 60)
    print(f"  Score: {summary['score']}")
    print()


if __name__ == "__main__":
    run_eval()
