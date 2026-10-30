"""Executes the `/health/index` checker embedded in ``.github/workflows/uptime.yml``.

That script is the only thing watching production between deploys, and until
#264 nothing ran it outside a real 30-minute probe. This repo's own record is
that a guard nothing executes is not a guard — a selection-count check once
passed on a suite where every test skipped, and another was satisfied by a code
comment. So this EXECUTES the script rather than grepping it, and asserts the
exit status and the reason text a human would be paged with.

The script is extracted through the YAML parser, not by string-slicing the file,
so a step rename or reflow cannot silently make these tests exercise nothing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "uptime.yml"


def _index_health_script() -> str:
    """Return the python embedded in the `/health/index` step of the workflow."""
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = [
        step
        for job in doc["jobs"].values()
        for step in job.get("steps", [])
        if "/health/index" in step.get("run", "")
    ]
    assert len(steps) == 1, f"expected exactly one /health/index step, found {len(steps)}"
    run = steps[0]["run"]
    start = run.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n")
    end = run.index("\nPY", start)
    body = textwrap.dedent(run[start:end])
    # Partner assertion: the extraction really did capture the checker, so a
    # future reflow that yields an empty or unrelated string fails here rather
    # than making every case below pass vacuously.
    assert "problems" in body and "vector_arm" in body
    return body


def _run(body: dict[str, Any], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "check.py"
    script.write_text(_index_health_script())
    (tmp_path / "index.json").write_text(json.dumps(body))
    # The script opens the hard-coded path the workflow's curl writes to.
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            _index_health_script().replace('"/tmp/index.json"', repr(str(tmp_path / "index.json"))),
        ],
        capture_output=True,
        text=True,
    )


_OK_EMBEDDER: dict[str, Any] = {"state": "ok", "refused_since": None}


def _arm(**over: Any) -> dict[str, Any]:
    arm = {
        "status": "healthy",
        "healthy": True,
        "embedded_ratio": 1.0,
        "embedder_match": True,
        "active_index_count": 1,
    }
    arm.update(over)
    return {"status": "ready", "vector_arm": arm, "query_embedder": _OK_EMBEDDER}


@pytest.mark.parametrize(
    ("name", "body", "expected_exit", "expected_reason"),
    [
        ("healthy", _arm(), 0, ""),
        (
            "ambiguous",
            _arm(
                status="ambiguous",
                healthy=False,
                embedded_ratio=None,
                embedder_match=False,
                active_index_count=2,
            ),
            1,
            "AMBIGUOUS",
        ),
        ("dead", _arm(status="dead", healthy=False, embedded_ratio=0.0), 1, "DEAD"),
        (
            "genuine mismatch",
            _arm(status="mismatch", healthy=False, embedder_match=False),
            1,
            "embedders disagree",
        ),
        ("pre_index", {"status": "pre_index", "vector_arm": None}, 1, "pre_index"),
        # The body production emits TODAY, from a backend that predates #264 and
        # so has no `active_index_count`. A new workflow must not page on it.
        (
            "old backend, no active_index_count",
            {
                "status": "ready",
                "vector_arm": {
                    "status": "healthy",
                    "healthy": True,
                    "embedded_ratio": 1.0,
                    "embedder_match": True,
                },
                "query_embedder": _OK_EMBEDDER,
            },
            0,
            "",
        ),
    ],
)
def test_uptime_index_health_probe(
    name: str, body: dict[str, Any], expected_exit: int, expected_reason: str, tmp_path: Path
) -> None:
    """Every state the probe must distinguish, executed end to end."""
    result = _run(body, tmp_path)
    assert result.returncode == expected_exit, f"{name}: {result.stdout}{result.stderr}"
    if expected_reason:
        assert expected_reason in result.stderr, f"{name}: reason was {result.stderr!r}"


def test_ambiguous_alert_does_not_also_blame_the_embedders(tmp_path: Path) -> None:
    """The ambiguous alert must not carry the mismatch line as well (#264).

    Under `ambiguous` the route feeds the shared predicate a sentinel rather than
    a stamp, so `embedder_match` is `False` whatever the real identities are — it
    is not a comparison. Reporting "query/index embedders disagree" alongside
    would send the operator to ADR-0003 instead of to `promote_version`.

    Turns RED if the `and arm_status != "ambiguous"` condition is dropped.
    """
    result = _run(
        _arm(
            status="ambiguous",
            healthy=False,
            embedded_ratio=None,
            embedder_match=False,
            active_index_count=2,
        ),
        tmp_path,
    )
    assert result.returncode == 1
    assert "AMBIGUOUS" in result.stderr
    assert "embedders disagree" not in result.stderr


def test_a_genuine_embedder_mismatch_is_still_reported(tmp_path: Path) -> None:
    """The partner to the suppression above: the real case must still page.

    Without this, the suppression could be widened to drop the mismatch check
    entirely and nothing would notice.
    """
    result = _run(_arm(status="mismatch", healthy=False, embedder_match=False), tmp_path)
    assert result.returncode == 1
    assert "embedders disagree" in result.stderr


def _with_embedder(state: Any, since: str | None = None) -> dict[str, Any]:
    body = _arm()
    body["query_embedder"] = {"state": state, "refused_since": since}
    return body


@pytest.mark.parametrize(
    ("name", "body", "expected_exit", "expected_reason"),
    [
        ("embedder ok", _with_embedder("ok"), 0, ""),
        # The empty-prepaid-balance case: search has silently dropped to
        # keyword-only while answers keep coming. This is the page, and it says
        # what to do and how to clear it.
        (
            "embedder refused",
            _with_embedder("refused", "2026-10-30T12:00:00+00:00"),
            1,
            "the provider has refused this account since 2026-10-30T12:00:00+00:00",
        ),
        (
            "embedder refused, clearing step",
            _with_embedder("refused"),
            1,
            "clears by itself within about 3.5 hours",
        ),
        # Production always sets Redis: without it this alert is off, so say so.
        ("embedder unconfigured", _with_embedder("unconfigured"), 1, "no Redis"),
        ("embedder unavailable", _with_embedder("unavailable"), 1, "could not read Redis"),
        # Decided on the known-good value (#460): anything else pages.
        ("embedder unrecognised state", _with_embedder("degraded"), 1, "unrecognised"),
        # The state an earlier build reported for "not tracked"; it must not
        # creep back in as a pass.
        ("embedder old unknown state", _with_embedder("unknown"), 1, "unrecognised"),
        (
            "embedder refused, bad time",
            _with_embedder("refused", "not-a-timestamp"),
            1,
            "<invalid>",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else "",
)
def test_query_embedder_state(
    tmp_path: Path, name: str, body: dict[str, Any], expected_exit: int, expected_reason: str
) -> None:
    """Red when: the probe stops reading ``query_embedder``, passes anything but
    "ok", or loses the reason text the owner acts on."""
    result = _run(body, tmp_path)
    assert result.returncode == expected_exit, (name, result.stdout, result.stderr)
    if expected_reason:
        assert expected_reason in result.stderr


def test_a_refusal_is_annotated_on_the_run_page(tmp_path: Path) -> None:
    """Red when: the refusal is only in the job log. GitHub's failure email
    names the step; an ``::error`` line puts the reason on the run page."""
    result = _run(_with_embedder("refused", "2026-10-30T12:00:00+00:00"), tmp_path)
    assert any(line.startswith("::error ") for line in result.stdout.splitlines()), result.stdout


@pytest.mark.parametrize("missing", ["absent", "null"])
def test_a_body_without_the_block_pages(tmp_path: Path, missing: str) -> None:
    """Red when: a body without ``query_embedder`` passes. A rollback to a build
    that predates the block would otherwise switch this alert off silently."""
    body = _arm()
    if missing == "absent":
        del body["query_embedder"]
    else:
        body["query_embedder"] = None
    result = _run(body, tmp_path)
    assert result.returncode == 1
    assert "query_embedder block is missing" in result.stderr


def test_a_forged_refusal_time_cannot_write_a_workflow_command(tmp_path: Path) -> None:
    """Red when: the probe prints a refusal time it did not validate. A line in
    a GitHub Actions log that starts with ``::`` is a workflow command."""
    body = _with_embedder("refused", "x\n::warning::forged")
    result = _run(body, tmp_path)
    assert result.returncode == 1
    out = result.stdout + result.stderr
    assert "::warning::forged" not in out
    assert "<invalid>" in out


@pytest.mark.parametrize(
    "body",
    [
        _with_embedder("refused", "2026-10-30\n::warning::forged"),
        # fromisoformat accepts any separator; the probe must not.
        _with_embedder("refused", "2026-10-30\n12:00:00"),
        {**_arm(), "status": "ready\n::warning title=x::injected"},
    ],
    ids=["refused-since", "iso-separator", "status-field"],
)
def test_no_body_field_starts_a_workflow_command(tmp_path: Path, body: dict[str, Any]) -> None:
    """Red when: a field from the response body is printed raw. A line in a
    GitHub Actions log that starts with ``::`` is a workflow command."""
    result = _run(body, tmp_path)
    lines = (result.stdout + result.stderr).splitlines()
    assert not any(line.startswith("::warning") for line in lines), lines
