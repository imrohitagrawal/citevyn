"""The shipped-corpus judged eval after its second review round (#598 Step 4).

Free and hermetic. Each test names the change that turns it red.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.eval import grader_check as gc

DISPATCH_REF = "refs/heads/eval/598-step4"


def test_the_capped_dispatch_refusal_rounds_the_minutes_left_down() -> None:
    """Red when: the minutes left are rounded to nearest, so 69.5 minutes left reads
    "only 70 minutes are left ... needs 70" (a refusal that contradicts itself)."""
    now = datetime(2026, 11, 16, 22, 50, 30, tzinfo=UTC)
    refusal = gc.capped_dispatch_refusal(capped=True, now=now)
    assert refusal is not None  # partner: 69.5 minutes left is refused
    assert refusal.startswith("only 69 minutes are left before 00:00 UTC"), refusal
    assert "only 70 " not in refusal


def test_the_grader_check_refusal_rounds_the_minutes_left_down() -> None:
    """Red when: the grader check's refusal rounds 249.5 minutes left up to "250",
    the very number it says it needs."""
    now = datetime(2026, 11, 16, 19, 50, 30, tzinfo=UTC)
    refusal = gc.start_refusal(ref=DISPATCH_REF, now=now)
    assert refusal is not None  # partner: 249.5 minutes left is refused
    assert refusal.startswith("only 249 minutes are left before 00:00 UTC"), refusal
    assert "only 250 " not in refusal
