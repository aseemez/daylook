"""Opt-in test that the evaluation harness scores a perfect run.

Skipped unless ``RUN_LLM_TESTS=1`` (the first run makes real model calls; subsequent runs are
cache-served). Asserts all five criteria pass.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run live model tests"
)


def test_eval_scores_five_of_five():
    import eval as eval_module

    summary = eval_module.run_eval(verbose=False)
    assert summary["passed"] == summary["total"], summary["detail"]
    assert summary["total"] == 5
