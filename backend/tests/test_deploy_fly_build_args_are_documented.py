"""The Fly deploy command must pass every build arg baked into the bundle (#296).

WHY THIS FILE EXISTS
--------------------
`infra/docker/Dockerfile.api` builds the browser bundle inside the image and
threads build arguments into Vite, which bakes them into the JS at BUILD time.
`fly.toml`'s ``[build.args]`` sets only ``VERSION``. So the ONLY thing standing
between a deploy and a wrong baked value is the ``--build-arg`` list an
operator copies out of ``docs/DEPLOY_FLY.md`` §4.1.

On 2026-09-02 that list was missing ``VITE_API_DEMO_KEY``. The image built
fine, ``/health`` stayed green, and every browser call 401'd for about an hour
(release v6, fixed by v7) — #296.

Prose cannot hold this. The runbook already *warned* about the failure in three
separate paragraphs while its own verification snippet was blind to it. This is
the mechanical half: if someone adds a build argument to the Dockerfile's
frontend stage, or deletes one from the deploy command, a test goes red.

WHY THE REQUIRED SET IS DERIVED, NOT LISTED
-------------------------------------------
A hard-coded list of "args that matter" rots exactly like the docs it guards --
the next argument arrives undocumented and the list still passes. So the set is
read out of the Dockerfile's ``RUN ... npm run build`` line: anything threaded
into that command is, by construction, baked into what browsers download and
therefore must be chosen deliberately at deploy time.

WHY `fly.toml` IS NOT ACCEPTED AS AN ALTERNATIVE HOME
------------------------------------------------------
It would be a reasonable place for ``VERSION``, but ``VITE_API_DEMO_KEY`` is a
credential: ``fly.toml`` is tracked in git, and
``test_fly_config.py::test_fly_toml_has_no_plaintext_credentials`` already
forbids putting it there. The CLI argument, read from the running machine, is
the only correct route -- so the runbook is the only place this can be
asserted.

SCOPE, STATED HONESTLY
----------------------
This proves the deploy COMMAND is complete and cannot silently pass an empty
value. It cannot prove the operator ran it, nor that the value was right --
that is ``scripts/check_bundle_key.sh``, which inspects the SERVED bundle, and
``tests/shell/test_check_bundle_key.sh``, which tests that checker. The two
halves are deliberately separate: this one runs in CI on every push, that one
needs a deployed site.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "infra" / "docker" / "Dockerfile.api"
_RUNBOOK = _REPO_ROOT / "docs" / "DEPLOY_FLY.md"


def _baked_build_args() -> list[str]:
    """Build args threaded into the frontend's ``npm run build``.

    These end up inside the JS every visitor downloads, so each one is a
    deploy-time decision rather than an implementation detail.

    Read out of the Dockerfile text rather than by running docker: the point is
    the contract between two files an operator reads, and a test that needs a
    daemon would not run in CI.
    """
    src = _DOCKERFILE.read_text(encoding="utf-8")
    # The RUN command continues across backslash-newlines; join them first so
    # the whole invocation is one string.
    joined = re.sub(r"\\\n\s*", " ", src)
    run_line = next(
        (
            line
            for line in joined.splitlines()
            if line.startswith("RUN ") and "npm run build" in line
        ),
        None,
    )
    if run_line is None:
        return []
    # `RUN VITE_API_DEMO_KEY="${VITE_API_DEMO_KEY}" VITE_API_LIVE="${...}" npm run build`
    # -- the env assignments that precede the command.
    prefix = run_line.split("npm run build")[0]
    return re.findall(r'\b([A-Z][A-Z0-9_]*)="\$\{\1\}"', prefix)


def _deploy_command() -> str:
    """The ``fly deploy`` shell block from §4.1, and nothing else.

    Scoped to the section on purpose. Searching the whole file would pass on a
    prose mention of the argument (§4.1 has three) and would also scan §6.1's
    unrelated ``fly deploy --image`` rollback command, which legitimately takes
    no build arguments because it redeploys an image that is already built.
    """
    text = _RUNBOOK.read_text(encoding="utf-8")
    section = re.search(r"^### 4\.1 [^\n]*\n(.*?)(?=^#{2,3} )", text, re.S | re.M)
    if section is None:
        return ""
    blocks = re.findall(r"^```bash\n(.*?)^```", section.group(1), re.S | re.M)
    return next((b for b in blocks if "fly deploy" in b), "")


# ---------------------------------------------------------------------------
# Partners. Every assertion below is of the form "X is present in Y". Each one
# passes trivially if the extraction returns nothing, so the extractions are
# pinned first -- otherwise a Dockerfile reformat or a heading rename would
# silently reduce this file to zero guarded cases and still report green.
# ---------------------------------------------------------------------------


def test_the_dockerfile_extraction_finds_the_baked_args_at_all() -> None:
    args = _baked_build_args()
    assert len(args) >= 2, f"expected at least 2 baked build args, found {args}"
    assert "VITE_API_DEMO_KEY" in args, (
        f"VITE_API_DEMO_KEY is the argument whose absence caused #296; if it is no "
        f"longer threaded into the frontend build this guard has lost its subject. "
        f"Found: {args}"
    )


def test_the_runbook_extraction_finds_the_deploy_command_at_all() -> None:
    cmd = _deploy_command()
    assert cmd, "no ```bash block containing 'fly deploy' found under '### 4.1'"
    assert "--build-arg" in cmd, f"§4.1's deploy command passes no build args at all:\n{cmd}"


def test_the_extraction_is_scoped_to_section_4_1() -> None:
    """The rollback command in §6.1 must not be what we are reading.

    Without this, a future edit that broadens the section regex would start
    matching ``fly deploy --image``, which passes no build args -- and the
    guard would fail confusingly, or worse, pass against the wrong command.
    """
    cmd = _deploy_command()
    assert "--image" not in cmd, (
        "§4.1 extraction picked up the §6.1 rollback command (`fly deploy --image`)"
    )
    assert "--app citevyn" in cmd


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arg", _baked_build_args() or ["<extraction-failed>"])
def test_every_baked_build_arg_is_passed_by_the_deploy_command(arg: str) -> None:
    """A prose mention does not count -- it must be an actual `--build-arg`."""
    cmd = _deploy_command()
    assert f"--build-arg {arg}=" in cmd, (
        f"docs/DEPLOY_FLY.md §4.1 does not pass `--build-arg {arg}=`.\n"
        f"{arg} is threaded into `npm run build` in infra/docker/Dockerfile.api, so "
        f"its value is baked into the bundle every visitor downloads. Omitting it "
        f"ships the Dockerfile's default -- which for VITE_API_DEMO_KEY is the "
        f"publicly-known `local-demo-key`, and production rejects it with 401 on "
        f"every browser call while /health stays green. That is #296.\n"
        f"The deploy command found was:\n{cmd}"
    )


def test_no_build_arg_can_silently_receive_an_empty_value() -> None:
    """Every variable-sourced `--build-arg` must use the fail-on-empty form.

    This is the second half of #296, and the subtler one. The runbook used to
    read the key with an inline `$(fly ssh console ...)`. When the machine has
    scaled to zero that substitution yields an EMPTY STRING, and -- measured
    with docker -- `--build-arg NAME=""` does not fall back to the ARG default:
    it leaves the argument empty. Vite bakes `""` (the `??` in
    `frontend/src/lib/api.ts` does not fire on an empty string), the browser
    sends a bare `Authorization: Bearer `, and production 401s exactly as in
    v6. A command substitution has no way to fail on empty; `${VAR:?msg}` exits
    the shell before `fly deploy` runs, in both bash and zsh.
    """
    cmd = _deploy_command()
    offenders: list[str] = []
    for name, value in re.findall(r'--build-arg ([A-Z][A-Z0-9_]*)=("[^"]*"|\S+)', cmd):
        if "$(" in value:
            offenders.append(f"{name}: inline command substitution {value} cannot fail on empty")
        elif "${" in value and ":?" not in value:
            offenders.append(f"{name}: {value} uses an unguarded expansion; use ${{VAR:?msg}}")
    assert not offenders, (
        "docs/DEPLOY_FLY.md §4.1 can pass an empty build argument:\n  "
        + "\n  ".join(offenders)
        + "\nAssign to a shell variable first and reference it as "
        '`"${VAR:?why this is empty}"` so the deploy aborts instead of shipping '
        "a bundle whose baked value is the empty string (#296)."
    )


def _runnable_bash() -> str:
    """Every fenced bash block in the runbook, concatenated.

    Scoped to fenced blocks because that is what an operator copies and runs.
    Prose is commentary -- and this file must be able to *discuss* the check it
    replaced without tripping over the mention, which a whole-file substring
    search does. That is not hypothetical: the first version of the test below
    failed on the very paragraph explaining why the old check was removed.

    Blockquote markers are stripped first: §4.1 nests code inside `>` quotes,
    and an operator runs that code just the same.
    """
    text = _RUNBOOK.read_text(encoding="utf-8")
    unquoted = re.sub(r"^> ?", "", text, flags=re.M)
    return "\n".join(re.findall(r"^```bash\n(.*?)^```", unquoted, re.S | re.M))


def test_the_runbook_has_runnable_bash_blocks_at_all() -> None:
    """Partner for the two assertions below, which both check for absence."""
    blocks = _runnable_bash()
    assert len(blocks) > 500, f"expected substantial runnable bash, got {len(blocks)} chars"
    assert "fly deploy" in blocks


def test_the_blind_absence_check_has_not_come_back() -> None:
    """`grep -c local-demo-key ... must print 0` PASSES on the broken bundle.

    Measured: a bundle built with an empty `--build-arg VITE_API_DEMO_KEY`
    contains neither the default nor any key, so that check prints 0 and
    reports success on precisely the outage it was added to catch. It was
    replaced by `scripts/check_bundle_key.sh`, which asserts the PRESENCE of
    the expected non-empty key. This pins the replacement so a future edit does
    not quietly restore the check that could not see the failure.
    """
    blocks = _runnable_bash()
    assert "check_bundle_key.sh" in blocks, (
        "docs/DEPLOY_FLY.md no longer RUNS scripts/check_bundle_key.sh -- the "
        "post-deploy bundle verification has been removed, or demoted to prose."
    )
    assert (_REPO_ROOT / "scripts" / "check_bundle_key.sh").exists(), (
        "the runbook references scripts/check_bundle_key.sh but the script is missing"
    )
    assert "grep -c local-demo-key" not in blocks, (
        "the absence-based bundle check is back in docs/DEPLOY_FLY.md. It prints 0 -- "
        "reports success -- on a bundle built with an empty build arg, which is the "
        "#296 outage reached through a different string. Use "
        "scripts/check_bundle_key.sh, which asserts the expected key is PRESENT."
    )


def test_the_smoke_call_sends_the_session_cookie() -> None:
    """A cookie-less ask 404s, so a runbook that omits the jar does not work.

    Sessions are owned by the `__Host-citevyn_session` cookie, not the bearer
    (ADR-0004 PR 3). `resolve_principal` mints a fresh anonymous principal when
    no cookie arrives, so the second call owns nothing and the session created
    one line earlier comes back 404. The runbook's snippet had no jar and could
    never have worked as written.
    """
    blocks = _runnable_bash()
    ask = next(
        (b for b in blocks.split("\n\n") if "/messages" in b),
        "",
    )
    assert ask, "no runnable block posting to /messages found in docs/DEPLOY_FLY.md"
    assert '-b "$JAR"' in ask, (
        "the /messages call in docs/DEPLOY_FLY.md does not send the session cookie "
        f"(-b). Without it the call returns 404 not_found. Block:\n{ask}"
    )
    create = next((b for b in blocks.split("\n\n") if "/v1/sessions" in b and "-c " in b), "")
    assert create, (
        "the POST /v1/sessions call does not SAVE a cookie jar (-c), so there is "
        "nothing for the /messages call to send back."
    )
