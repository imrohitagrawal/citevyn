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
(release v6, fixed by v7) -- #296.

Prose cannot hold this. The runbook already *warned* about the failure in three
separate paragraphs while its own verification snippet was blind to it. This is
the mechanical half: if someone adds a build argument to the Dockerfile's
frontend stage, or deletes one from the deploy command, a test goes red.

WHY THE REQUIRED SET IS DERIVED, NOT LISTED
-------------------------------------------
A hard-coded list of "args that matter" rots exactly like the docs it guards --
the next argument arrives undocumented and the list still passes. So the set is
read out of the Dockerfile's frontend stage.

It is derived from the ``ARG`` DECLARATIONS, not from the ``RUN`` line's env
prefix. The first version of this file scanned the prefix for the exact
spelling ``NAME="${NAME}"``, and adversarial review defeated it twice with a
build arg that is still baked into the bundle: written unbraced as
``VITE_X="$VITE_X"``, and hoisted to ``ENV VITE_X=${VITE_X}`` (Docker's own
recommended style). Both left an undocumented baked argument with the suite
green -- the precise rot this design claims to prevent, reached through a
different quoting style. An ``ARG`` line has one spelling; a use site has many.

WHY COMMENTS ARE STRIPPED BEFORE EVERY ASSERTION
------------------------------------------------
Adversarial review satisfied three assertions in this file with a shell COMMENT
while the real mechanism was gone -- the runbook still *said* ``-b "$JAR"`` and
``check_bundle_key.sh`` in a comment above a command that did neither. That is
the repo's recorded "a guard satisfied by a comment" defect, and it is the same
shape as the bug this PR fixes. Everything below reads
:func:`_runnable_bash`, which deletes comment lines and joins backslash
continuations, so an assertion can only be satisfied by something an operator
would actually EXECUTE.

WHY `fly.toml` IS NOT ACCEPTED AS AN ALTERNATIVE HOME
------------------------------------------------------
It would be a reasonable place for ``VERSION``, but ``VITE_API_DEMO_KEY`` is a
credential: ``fly.toml`` is tracked in git, and ``test_fly_config.py`` already
forbids putting a plaintext credential there. The CLI argument, read from the
running machine, is the only correct route -- so the runbook is the only place
this can be asserted.

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

# Accepted `--build-arg` value shapes. This is a WHITELIST, deliberately.
#
# The first version was a blacklist -- it flagged `$(...)` and an unguarded
# `${...}` and accepted everything else. Two forms walked straight through,
# each reintroducing the exact defect it exists to prevent: a BACKTICK
# substitution (which contains neither "$(" nor "${") and a brace-less
# `"$DEMO_KEY"`. A guard that enumerates the BAD forms only ever catches the
# ones its author already thought of; enumerating the GOOD forms fails closed
# on everything else, including the next syntax nobody has thought of yet.
#
# The value alternation ends in `\S*`, not `\S+`, so that a bare
# `--build-arg VITE_API_DEMO_KEY=` (nothing at all after the `=`) is CAPTURED
# as an empty value and rejected below. With `\S+` the regex simply did not
# match that form, so the argument was never examined -- and the sibling test
# that looks for the literal `--build-arg NAME=` was satisfied by it. Between
# them, the most direct possible spelling of the #296 outage passed both
# guards. An extractor that silently skips its subject is worse than no
# extractor, because it reports green.
_BUILD_ARG_RE = re.compile(r'--build-arg ([A-Z][A-Z0-9_]*)=("(?:[^"\\]|\\.)*"|\S*)')

# `"${VAR:?message}"` and nothing else -- anchored to the WHOLE value, so a
# guarded expansion with anything appended (`"${VAR:?x}`evil`"`) is rejected.
# `[^{}]*` in the message keeps a nested `${OTHER:-default}` out.
_GUARDED_EXPANSION_RE = re.compile(r'^"\$\{[A-Za-z_][A-Za-z0-9_]*:\?[^{}]*\}"$')

# A NON-EMPTY literal with no expansion syntax at all: `true`, `dev`, `1.2.3`,
# optionally quoted with BALANCED quotes.
#
# The `+` and the balanced alternation are both load-bearing. The first version
# of this whitelist used `^"?[A-Za-z0-9_.:/-]*"?$`, whose `*` matched the EMPTY
# string -- so `--build-arg VITE_API_DEMO_KEY=""` was accepted as a "plain
# literal". That is the #296 outage written verbatim into the runbook, passed
# by the guard whose entire purpose is to prevent it: the deny-by-default
# rewrite had reintroduced, as a literal, the exact defect it was closing in
# its expanded form. A lone `"` slipped through the same way.
_PLAIN_LITERAL_RE = re.compile(r'^(?:"[A-Za-z0-9_.:/-]+"|[A-Za-z0-9_.:/-]+)$')


def _frontend_stage() -> str:
    """The Dockerfile text from `AS frontend` up to the next stage.

    Scoped so a `VERSION` ARG in the builder/runtime stages -- which is not
    baked into the browser bundle and is already handled -- is not swept in.
    """
    src = _DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r"^FROM .* AS frontend\n(.*?)(?=^FROM )", src, re.S | re.M)
    return match.group(1) if match else ""


def _baked_build_args() -> list[str]:
    """Every ``ARG`` declared in the frontend stage.

    These are, by construction, the knobs the browser bundle can be built
    against, so each is a deploy-time decision rather than an implementation
    detail. Read from the Dockerfile text rather than by running docker: the
    subject is the contract between two files an operator reads, and a test
    needing a daemon would not run in CI.
    """
    return re.findall(r"^ARG ([A-Z][A-Z0-9_]*)", _frontend_stage(), re.M)


def _runnable_bash(section: str | None = None) -> str:
    """Fenced bash the operator would actually execute, comments removed.

    Three transformations, each load-bearing:

    * only ```` ```bash ```` fences -- prose is commentary, and this file must be
      able to DISCUSS the check it replaced without tripping over the mention.
      A whole-file substring search cannot: the first version of
      :func:`test_the_blind_absence_check_has_not_come_back` failed on the very
      paragraph explaining why the old check was removed.
    * blockquote markers stripped -- §4.1 nests code inside ``>`` quotes and an
      operator runs that code just the same.
    * COMMENT LINES DELETED and backslash continuations joined -- so an
      assertion is satisfied only by a real command. Review defeated three
      assertions here by moving the required text into a comment above a
      command that no longer did the thing.
    """
    text = _RUNBOOK.read_text(encoding="utf-8")
    if section is not None:
        match = re.search(
            rf"^### {re.escape(section)} [^\n]*\n(.*?)(?=^#{{2,3}} )", text, re.S | re.M
        )
        text = match.group(1) if match else ""
    unquoted = re.sub(r"^> ?", "", text, flags=re.M)
    blocks = re.findall(r"^```bash\n(.*?)^```", unquoted, re.S | re.M)
    joined = re.sub(r"\\\n\s*", " ", "\n".join(blocks))
    return "\n".join(ln for ln in joined.splitlines() if not ln.lstrip().startswith("#"))


def _deploy_commands() -> list[str]:
    """EVERY `fly deploy` command line in §4.1, not just the first.

    Review hid a defective command behind a correct-looking decoy: the old
    implementation took the first matching block, so adding an illustrative
    block above the real one let the real one drop ``VITE_API_DEMO_KEY`` with
    the suite green -- reproducing release v6 in the command an operator
    copies. Asserting over all of them removes the hiding place.
    """
    return [ln for ln in _runnable_bash("4.1").splitlines() if "fly deploy" in ln]


# ---------------------------------------------------------------------------
# Partners. Every assertion below is "X is present in Y", which passes
# trivially if the extraction returns nothing. The extractions are pinned
# first -- otherwise a Dockerfile reformat or a heading rename would silently
# reduce this file to zero guarded cases and still report green.
# ---------------------------------------------------------------------------


def test_the_dockerfile_extraction_finds_the_baked_args_at_all() -> None:
    args = _baked_build_args()
    assert _frontend_stage(), "could not locate the `AS frontend` stage in Dockerfile.api"
    assert len(args) >= 2, f"expected at least 2 ARGs in the frontend stage, found {args}"
    assert "VITE_API_DEMO_KEY" in args, (
        f"VITE_API_DEMO_KEY is the argument whose absence caused #296; if it is no "
        f"longer declared in the frontend stage this guard has lost its subject. "
        f"Found: {args}"
    )


def test_every_declared_arg_is_actually_threaded_into_the_build() -> None:
    """The ARG list is only the right subject if the args reach Vite.

    Without this, someone could silence a required case by deleting the use
    site and leaving the declaration -- or, worse, leave a declared-but-unused
    ARG that this file then demands the runbook pass for no reason.
    """
    # Comments stripped before counting. The Dockerfile discusses these args at
    # length in comments, so counting raw occurrences would let a declared-but-
    # unused ARG be "used" by a sentence about it -- the same comment-satisfies-
    # the-guard defect this file strips comments elsewhere to avoid.
    stage = "\n".join(
        ln for ln in _frontend_stage().splitlines() if not ln.lstrip().startswith("#")
    )
    unused = [a for a in _baked_build_args() if stage.count(a) < 2]
    assert not unused, (
        f"declared in the frontend stage but never used: {unused}. Either thread them "
        f"into `npm run build` (or an ENV) or delete the ARG -- a declared-but-unused "
        f"build arg makes this guard demand a deploy flag that does nothing."
    )


def test_the_runbook_extraction_finds_the_deploy_command_at_all() -> None:
    cmds = _deploy_commands()
    assert cmds, "no runnable `fly deploy` line found under '### 4.1'"
    assert all("--build-arg" in c for c in cmds), (
        f"a §4.1 deploy command passes no build args at all:\n{cmds}"
    )


def test_the_extraction_is_scoped_to_section_4_1() -> None:
    """The rollback command in §6.1 must not be what we are reading.

    Without this, a future edit that broadened the section regex would start
    matching ``fly deploy --image``, which passes no build args -- so the guard
    would fail confusingly, or pass against the wrong command.
    """
    cmds = _deploy_commands()
    assert not any("--image" in c for c in cmds), (
        "§4.1 extraction picked up the §6.1 rollback command (`fly deploy --image`)"
    )
    # Asserted on the deploy LINE. Review showed a whole-block check was
    # satisfied by the `fly ssh console --app citevyn` line above it, so
    # removing `--app` from the deploy command alone stayed green.
    assert all("--app citevyn" in c for c in cmds), f"a §4.1 deploy command has no --app: {cmds}"


# ---------------------------------------------------------------------------
# The guard itself.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arg", _baked_build_args() or ["<extraction-failed>"])
def test_every_baked_build_arg_is_passed_by_the_deploy_command(arg: str) -> None:
    """A prose mention does not count -- it must be an actual `--build-arg`."""
    cmds = _deploy_commands()
    missing = [c for c in cmds if f"--build-arg {arg}=" not in c]
    assert cmds and not missing, (
        f"a docs/DEPLOY_FLY.md §4.1 deploy command does not pass `--build-arg {arg}=`.\n"
        f"{arg} is declared in Dockerfile.api's frontend stage, so its value is baked "
        f"into the bundle every visitor downloads. Omitting it ships the Dockerfile's "
        f"default -- which for VITE_API_DEMO_KEY is the publicly-known "
        f"`local-demo-key`, and production rejects it with 401 on every browser call "
        f"while /health stays green. That is #296.\nCommands found:\n" + "\n".join(cmds)
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
    offenders = [
        f"{name}={value}"
        for cmd in _deploy_commands()
        for name, value in _BUILD_ARG_RE.findall(cmd)
        if not _GUARDED_EXPANSION_RE.match(value) and not _PLAIN_LITERAL_RE.match(value)
    ]
    assert not offenders, (
        "docs/DEPLOY_FLY.md §4.1 can pass an empty build argument:\n  "
        + "\n  ".join(offenders)
        + "\nA build-arg value must be either a plain literal (no expansion at all) or "
        'a fully guarded `"${VAR:?why this is empty}"`, so the deploy aborts instead '
        "of shipping a bundle whose baked value is the empty string (#296)."
    )


def _classify(cmd: str) -> list[str]:
    """The offender list :func:`test_no_build_arg...` computes, for one command."""
    return [
        f"{name}={value}"
        for name, value in _BUILD_ARG_RE.findall(cmd)
        if not _GUARDED_EXPANSION_RE.match(value) and not _PLAIN_LITERAL_RE.match(value)
    ]


# Every value shape that has ever been proposed for this argument, and whether
# the classifier must reject it. The test above only ever sees the ONE shape
# the runbook currently uses, so on its own it proves almost nothing about the
# rule -- a hole in the regex is invisible until someone writes that form into
# the docs, which is precisely too late.
#
# Two entries are scars. `"$DEMO_KEY"` and the backtick form each walked
# through the original blacklist. `""` walked through the deny-by-default
# rewrite that replaced it, because the literal pattern's `*` matched the empty
# string -- the #296 outage accepted verbatim by the guard written to stop it.
_VALUE_SHAPES: tuple[tuple[str, bool], ...] = (
    ('"${DEMO_KEY:?the machine is asleep}"', True),
    ("true", True),
    ("dev", True),
    ('"1.2.3"', True),
    ('""', False),
    ("", False),
    ('"', False),
    ('"$(fly ssh console -C x)"', False),
    ("$(fly ssh console -C x)", False),
    ('"`fly ssh console -C x`"', False),
    ('"$DEMO_KEY"', False),
    ("$DEMO_KEY", False),
    ('"${DEMO_KEY}"', False),
    ("'${DEMO_KEY}'", False),
    ('"${DEMO_KEY:-local-demo-key}"', False),
    ('"${DEMO_KEY:?x}`evil`"', False),
    ('"${DEMO_KEY:?${OTHER:-x}}"', False),
)


@pytest.mark.parametrize(("value", "acceptable"), _VALUE_SHAPES, ids=lambda v: repr(v))
def test_the_build_arg_value_classifier_accepts_only_safe_shapes(
    value: str, acceptable: bool
) -> None:
    offenders = _classify(f"fly deploy --app citevyn --build-arg VITE_API_DEMO_KEY={value}")
    if acceptable:
        assert not offenders, f"{value!r} is safe but was rejected: {offenders}"
    else:
        assert offenders, (
            f"{value!r} was ACCEPTED. It cannot fail on empty (or is already empty), so "
            f"a deploy using it bakes an empty bearer into the bundle and every browser "
            f"call 401s while /health stays green -- #296."
        )


def test_the_runbook_has_runnable_bash_blocks_at_all() -> None:
    """Partner for the absence assertions below."""
    blocks = _runnable_bash()
    assert len(blocks) > 500, f"expected substantial runnable bash, got {len(blocks)} chars"
    assert "fly deploy" in blocks


def test_the_blind_absence_check_has_not_come_back() -> None:
    """`grep -c local-demo-key ... must print 0` PASSES on the broken bundle.

    Measured: a bundle built with an empty `--build-arg VITE_API_DEMO_KEY`
    contains neither the default nor any key, so that check prints 0 and
    reports success on precisely the outage it was added to catch. It was
    replaced by `scripts/check_bundle_key.sh`, which asserts the PRESENCE of
    the expected non-empty key.

    Both halves are structural rather than substring, because review defeated
    both: quoting the pattern (`grep -c 'local-demo-key'`) walked past a
    literal search, and demoting the replacement to a COMMENT satisfied its
    presence check while nothing ran it.
    """
    blocks = _runnable_bash()
    # `|` then anything but another pipe (the env-var prefix that passes the key)
    # then the script -- so it must be a real pipeline TARGET, not a mention.
    assert re.search(r"\|[^|\n]*\bcheck_bundle_key\.sh\b", blocks), (
        "docs/DEPLOY_FLY.md does not PIPE a bundle into scripts/check_bundle_key.sh. "
        "A mention in prose or a comment does not run it."
    )
    assert (_REPO_ROOT / "scripts" / "check_bundle_key.sh").exists(), (
        "the runbook references scripts/check_bundle_key.sh but the script is missing"
    )
    revived = [ln for ln in blocks.splitlines() if re.search(r"grep\b[^|]*local-demo-key", ln)]
    assert not revived, (
        "the absence-based bundle check is back in docs/DEPLOY_FLY.md:\n  "
        + "\n  ".join(revived)
        + "\nIt prints 0 -- reports success -- on a bundle built with an empty build "
        "arg, which is the #296 outage reached through a different string. Use "
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
    lines = _runnable_bash().splitlines()

    ask = next((ln for ln in lines if "/messages" in ln and "curl" in ln), "")
    assert ask, "no runnable curl posting to /messages found in docs/DEPLOY_FLY.md"
    assert '-b "$JAR"' in ask, (
        f"the /messages call does not SEND the session cookie (-b). Without it the "
        f"call returns 404 not_found. Line:\n{ask}"
    )

    # Selected by EXCLUDING /messages. Review showed that matching on
    # "/v1/sessions" alone fell through to the /messages line -- which contains
    # that substring and carries its own -c -- so deleting -c from the create
    # call left the suite green while no jar was ever saved.
    create = next(
        (ln for ln in lines if "/v1/sessions" in ln and "/messages" not in ln and "curl" in ln),
        "",
    )
    assert create, "no runnable curl creating a session (POST /v1/sessions) found"
    assert '-c "$JAR"' in create, (
        f"the POST /v1/sessions call does not SAVE a cookie jar (-c), so there is "
        f"nothing for the /messages call to send back. Line:\n{create}"
    )

    # The bearer must come from a variable the runbook actually sets. Review
    # found §4.4 using $CITEVYN_DEMO_API_KEY, which the runbook never assigns
    # in the operator's shell -- so the bearer was empty, /v1/sessions 401'd,
    # and the ask 404'd for a SECOND reason after the cookie fix.
    # Searched in the RUNNABLE bash, and only as a line that STARTS with the
    # assignment. Matching the whole document instead accepts
    # `fly secrets set ... CITEVYN_DEMO_API_KEY=...` from §3 -- which sets a
    # *Fly* secret, not a variable in the operator's shell. That false negative
    # is not hypothetical: it let this exact bypass through on the first
    # attempt at this assertion. Continuations are already joined by
    # `_runnable_bash`, so the `fly secrets set` block is one line beginning
    # with `fly`, and cannot satisfy the anchor.
    # Anchored at COLUMN 0 and excluding continuation lines. §3's block writes
    # `  CITEVYN_DEMO_API_KEY=... \` as an indented ARGUMENT to `fly secrets
    # set` -- it sets a Fly secret, not a shell variable, and its trailing `\`
    # survives the continuation join because a comment follows it. An indented,
    # backslash-terminated line is an argument; a real assignment the
    # operator's shell executes starts at column 0.
    assigned = "\n".join(
        ln for ln in _runnable_bash().splitlines() if not ln.rstrip().endswith("\\")
    )
    for line in (ask, create):
        bearer = re.search(r"Bearer \$\{?([A-Za-z_][A-Za-z0-9_]*)", line)
        assert bearer, f"no bearer variable in:\n{line}"
        name = bearer.group(1)
        assert re.search(rf"^(export )?{name}=", assigned, re.M), (
            f"the smoke call sends `Bearer ${name}` but no runnable block in "
            f"docs/DEPLOY_FLY.md ASSIGNS {name} in the operator's shell, so the bearer "
            f"is empty, POST /v1/sessions 401s, and the ask then 404s for a second "
            f"reason on top of the cookie one. (A `fly secrets set {name}=` line sets a "
            f"Fly secret, not a shell variable, and does not count.)"
        )


def test_the_citation_check_follows_redirects() -> None:
    """Without `-L`, three of five real corpus URLs report 301/308.

    Measured against the live corpus URLs: two `docs.anthropic.com` links
    answer 301 and `developers.openai.com` answers 308, so a bare
    `%{http_code}` tells the operator a healthy deploy has failed. That is this
    PR's own defect -- a check whose verdict does not match reality -- pointed
    the other way, so it gets a guard rather than a comment.
    """
    citation = [
        ln
        for ln in _runnable_bash().splitlines()
        if "curl" in ln and "%{http_code}" in ln and "$u" in ln
    ]
    assert citation, "no runnable citation-URL status check found in docs/DEPLOY_FLY.md"
    for line in citation:
        assert re.search(r"-[a-zA-Z]*L", line), (
            f"the citation check does not follow redirects (-L), so a healthy deploy "
            f"reports 301/308 for docs.anthropic.com and developers.openai.com and the "
            f"operator is told it failed. Line:\n{line}"
        )
