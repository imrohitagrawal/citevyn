"""Canonical filesystem locations for the eval harness.

The golden data lives at the repo root (``<repo>/tests/eval/golden.jsonl``),
mirroring the split the assertion golden runner already uses
(``<repo>/tests/golden/cases/``): data at the root, executor code under
``backend/tests/``.  Resolving relative to this file means
``python -m tests.eval.runner`` works from any CWD.
"""

from __future__ import annotations

import pathlib

# backend/tests/eval/paths.py -> parents[3] == repo root
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
EVAL_DIR = REPO_ROOT / "tests" / "eval"
GOLDEN_PATH = EVAL_DIR / "golden.jsonl"
# Distractor corpus golden (#125, PR B): multi-relevant + single-relevant cases evaluated
# ONLY by the opt-in Postgres distractor eval. Kept in its own file so it never enters the
# locked hermetic/judged run's case list nor the main-golden coverage assertions.
DISTRACTOR_GOLDEN_PATH = EVAL_DIR / "golden_distractors.jsonl"
DEFAULT_REPORT_PATH = pathlib.Path("artifacts/eval_report.json")
