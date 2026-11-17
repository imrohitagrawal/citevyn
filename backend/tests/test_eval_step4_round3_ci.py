"""The judged CI job after the third review round of #598 Step 4: the
``capped_baseline`` read fails closed.

On a ``workflow_dispatch`` the eval step reads the input once from the event payload.
If ``jq`` is missing, the payload cannot be read, or the value is anything but true or
false (case ignored), the step prints one ``::error::`` line naming the cause (never
the payload) and fails before the eval starts, so a paid baseline can never run
uncapped by accident. Push and tag runs do not read it. The step's script is run as
Actions would (bash -e) with a ``PATH`` of this test's own, so ``jq`` can be left out.

Each test names the change that turns it red.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import stat
import subprocess
from typing import Any

import pytest

from tests.test_grader_check_ci import _eval_step, _job, _render

BRANCH = "refs/heads/eval/598-step4"
_ABSENT = object()


def _step(
    tmp: pathlib.Path,
    *,
    event: str = "workflow_dispatch",
    capped: Any = _ABSENT,
    payload: str | None = None,
    with_jq: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str] | None]:
    """Run the eval step with the switch off. ``payload`` replaces the event file's
    text (``""`` leaves the file missing). Returns the process and the environment
    the fake ``uv`` saw (``None`` when the eval never started)."""
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "uv"
    fake.write_text('#!/bin/bash\n/usr/bin/env > "$CAPTURE_ENV"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    if with_jq:
        jq = shutil.which("jq")
        assert jq, "jq is not installed on this machine"
        (bin_dir / "jq").symlink_to(jq)
    event_path = tmp / "event.json"
    if payload is None:
        body: dict[str, Any] = {"ref": BRANCH}
        if event == "workflow_dispatch":
            body["inputs"] = {"grader_check": False}
            if capped is not _ABSENT:
                body["inputs"]["capped_baseline"] = capped
        event_path.write_text(json.dumps(body), encoding="utf-8")
    elif payload:
        event_path.write_text(payload, encoding="utf-8")
    grader_check = "false" if event == "workflow_dispatch" else ""
    env = {
        # Only this test's directory: no jq unless it was linked in above.
        "PATH": str(bin_dir),
        "HOME": str(tmp),
        "GITHUB_REF": BRANCH,
        "GITHUB_EVENT_NAME": event,
        "GITHUB_EVENT_PATH": str(event_path),
        "CAPTURE_ENV": str(tmp / "env.txt"),
    }
    for block in (_job().get("env") or {}, _eval_step().get("env") or {}):
        for name, value in block.items():
            env[str(name)] = _render(str(value), grader_check=grader_check, ref=BRANCH)
    script = tmp / "step.sh"
    script.write_text(
        _render(str(_eval_step()["run"]), grader_check=grader_check, ref=BRANCH),
        encoding="utf-8",
    )
    done = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    captured: dict[str, str] | None = None
    if (tmp / "env.txt").exists():
        captured = {}
        for line in (tmp / "env.txt").read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            captured[name] = value
    return done, captured


def _refused(done: subprocess.CompletedProcess[str], captured: dict[str, str] | None) -> str:
    """The one ``::error::`` line of a step that failed before the eval started."""
    assert done.returncode != 0, done.stdout + done.stderr
    assert captured is None, "the eval started"
    errors = [ln for ln in done.stdout.splitlines() if ln.startswith("::error")]
    assert len(errors) == 1, done.stdout + done.stderr
    return errors[0]


def test_a_dispatch_without_jq_fails_before_the_eval(tmp_path: pathlib.Path) -> None:
    """Red when: with ``jq`` missing, a dispatch that ticked ``capped_baseline`` runs
    uncapped and outside the window instead of failing with one error line."""
    done, captured = _step(tmp_path, capped=True, with_jq=False)
    line = _refused(done, captured)
    assert "capped_baseline" in line, line


@pytest.mark.parametrize("payload", ["", "{not json"], ids=["missing", "not-json"])
def test_an_unreadable_payload_fails_before_the_eval(tmp_path: pathlib.Path, payload: str) -> None:
    """Red when: a dispatch whose event payload is missing or not JSON runs uncapped,
    or the error line repeats the payload."""
    done, captured = _step(tmp_path, payload=payload)
    line = _refused(done, captured)
    assert "capped_baseline" in line, line
    assert "{not json" not in line


def test_a_value_other_than_true_or_false_fails_before_the_eval(tmp_path: pathlib.Path) -> None:
    """Red when: ``capped_baseline: "maybe"`` runs uncapped, or the error line repeats
    the value (it comes from the payload)."""
    done, captured = _step(tmp_path, capped="maybe")
    line = _refused(done, captured)
    assert "maybe" not in line, line


@pytest.mark.parametrize(
    "value", [True, "true", "True", "TRUE"], ids=["json", "lower", "title", "upper"]
)
def test_true_in_any_case_is_capped(tmp_path: pathlib.Path, value: Any) -> None:
    """Red when: the value is not lower-cased ("True" would fail or run uncapped), or
    JSON true stops reading as capped."""
    done, captured = _step(tmp_path, capped=value)
    assert done.returncode == 0, done.stdout + done.stderr
    assert captured is not None  # partner: the eval started
    assert captured.get("CITEVYN_COST_HARD_DAILY_USD") == "0.14"
    assert captured.get("CITEVYN_EVAL_CAPPED_DISPATCH") == "true"


@pytest.mark.parametrize(
    "value", [_ABSENT, False, "false", "False"], ids=["absent", "json", "lower", "title"]
)
def test_false_or_absent_runs_uncapped(tmp_path: pathlib.Path, value: Any) -> None:
    """Partner. Red when: a dispatch without the input, or with it false, fails or is
    capped (a release re-run would be blocked or held)."""
    done, captured = _step(tmp_path, capped=value)
    assert done.returncode == 0, done.stdout + done.stderr
    assert captured is not None
    assert "CITEVYN_COST_HARD_DAILY_USD" not in captured
    assert "CITEVYN_EVAL_CAPPED_DISPATCH" not in captured


def test_a_push_run_does_not_read_the_input(tmp_path: pathlib.Path) -> None:
    """Red when: a push or tag run (no inputs) fails because the input cannot be read,
    here with ``jq`` missing: a release's own judged run would be blocked."""
    done, captured = _step(tmp_path, event="push", with_jq=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert captured is not None
    assert "CITEVYN_COST_HARD_DAILY_USD" not in captured
    assert "CITEVYN_EVAL_CAPPED_DISPATCH" not in captured


def test_the_payload_is_never_printed(tmp_path: pathlib.Path) -> None:
    """Red when: the step echoes the event payload (it can carry any input text)."""
    marker = "payload-marker-not-for-logs"
    payload = json.dumps({"ref": BRANCH, "inputs": {"capped_baseline": marker}})
    done, _captured = _step(tmp_path, payload=payload)
    assert done.returncode != 0  # partner: the value was rejected
    assert marker not in done.stdout + done.stderr
