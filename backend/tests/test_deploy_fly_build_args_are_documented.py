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
import shlex
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "infra" / "docker" / "Dockerfile.api"
_RUNBOOK = _REPO_ROOT / "docs" / "DEPLOY_FLY.md"
_CONFIG = _REPO_ROOT / "backend" / "app" / "core" / "config.py"


def _production_rejected_defaults() -> set[str]:
    """The publicly-known defaults `Settings` names as weak in production.

    A SUBSET of what production refuses, not the whole of it: `_is_weak_secret`
    also imposes a 16-character floor, which this does not reproduce. Stated
    plainly because the first version of this docstring claimed the full set.

    Derived from `config.py`'s `_is_weak_secret(..., default="...")` calls
    rather than listed here, for the same reason the build-arg set is derived:
    a hand-kept copy rots, and the next publicly-known default would be
    accepted.

    A build arg may not be passed as a LITERAL equal to one of these. Review
    wrote `--build-arg VITE_API_DEMO_KEY=local-demo-key` into §4.1 and every
    test stayed green -- a "plain literal with no expansion", and also the
    exact value that produced release v6.
    """
    return set(re.findall(r'default="(local-[a-z-]+)"', _CONFIG.read_text(encoding="utf-8")))


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
# The `(?=\s|$)` after the quoted alternative is load-bearing. Without it the
# capture stopped at the first closing quote, so anything appended was never
# seen by any rule: `"${DEMO_KEY:?x}"$(fly ssh ...)` and `"1.2.3"$(evil)` both
# classified as safe on the truncated capture, re-admitting the one construct
# the whitelist exists to exclude. Anchoring a pattern to a value you did not
# fully capture anchors nothing.
_BUILD_ARG_RE = re.compile(r'--build-arg ([A-Z][A-Z0-9_]*)=("(?:[^"\\]|\\.)*"(?=\s|$)|\S*)')

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
    # Case-insensitive (`as frontend` is legal Docker) and `\Z`-terminated, so
    # the extraction does not silently empty out if the frontend stage ever
    # becomes the last stage in the file.
    match = re.search(r"^FROM .*\bAS\s+frontend\s*$\n(.*?)(?=^FROM |\Z)", src, re.S | re.M | re.I)
    return match.group(1) if match else ""


def _baked_build_args() -> list[str]:
    """Every ``ARG`` declared in the frontend stage.

    These are, by construction, the knobs the browser bundle can be built
    against, so each is a deploy-time decision rather than an implementation
    detail. Read from the Dockerfile text rather than by running docker: the
    subject is the contract between two files an operator reads, and a test
    needing a daemon would not run in CI.
    """
    # `\s+`, not a single space: `ARG  NAME` and `ARG\tNAME` are identical to
    # Docker, and review slipped a new baked argument past the one-space form
    # -- the same rot this derivation exists to prevent, through whitespace.
    return re.findall(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", _frontend_stage(), re.M)


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
    stripped = [_strip_comment(ln) for ln in "\n".join(blocks).splitlines()]
    joined = re.sub(r"\\\n\s*", " ", "\n".join(stripped))
    return "\n".join(ln for ln in joined.splitlines() if ln.strip())


def _lexes(head: str) -> bool:
    """True when `head` ends OUTSIDE any quoting -- i.e. a `#` here is a comment.

    `shlex` answers this for plain quoting, but it has no concept of command
    substitution: inside `"$( ... )"` the shell re-parses, so a `#` there IS a
    comment even though the enclosing double quote is still open. `shlex` sees
    only the open quote and says "quoted", which let
    `"$(curl -sS  # -L -w ...)"` keep its comment and satisfy the redirect
    assertion.

    So when the whole head does not lex, retry from each `$(` in turn, longest
    first: if the text since some command substitution opened lexes cleanly,
    the position is unquoted within that substitution. A `#` genuinely inside
    quotes -- `"$(echo 'a # b')"` -- still fails every retry, because the inner
    quote is unterminated in all of them.
    """
    candidates = [head]
    idx = head.rfind("$(")
    while idx != -1:
        candidates.append(head[idx + 2 :])
        idx = head.rfind("$(", 0, idx)
    for text in candidates:
        try:
            shlex.split(text)
        except ValueError:
            continue
        return True
    return False


def _strip_comment(line: str) -> str:
    """Truncate `line` at the start of its shell comment, preserving quoting.

    Two rules, from two sources, because neither alone is faithful.

    WORD-INITIAL is mine: `#` starts a comment only at the start of a line or
    after whitespace. `shlex` does not know this -- it has no concept of
    `${VAR#prefix}`, `${VAR##*/}` or `$((1#2))` and truncates all three -- so
    delegating wholesale would corrupt legitimate commands.

    UNQUOTED is `shlex`'s: whether that `#` sits inside quoting. This is the
    half I kept getting wrong. Three review rounds each found another POSIX
    rule I had re-derived incorrectly -- `$(` re-entering a fresh quoting
    context, backslash escapes inside double quotes, and then backslash escapes
    OUTSIDE them, where `--label=say\\"hi  # --build-arg ...` kept its comment
    and passed every test while bash dropped the argument. Rather than patch a
    hand-written lexer a fourth time, the quoting question is answered by
    asking `shlex` whether the text BEFORE the `#` lexes: an unterminated quote
    means the `#` is inside one.

    The raw string is cut rather than rebuilt from tokens because the value
    checks need the original text -- `"${V:?x}"` with its quotes intact.

    Why a guard here matters at all: a comment satisfying an assertion is this
    repo's recorded defect, and the same shape as the bug this PR fixes.
    """
    raw = line.rstrip()
    trailing = ""
    # A line-continuation backslash makes shlex raise ("No escaped character")
    # and the runbook is full of them. Set it aside; restore it only if no
    # comment precedes it, since otherwise it was inside the comment.
    if raw.endswith("\\") and not raw.endswith("\\\\"):
        trailing, raw = " \\", raw[:-1]

    candidates = [i for i, ch in enumerate(raw) if ch == "#" and (i == 0 or raw[i - 1].isspace())]
    for i in candidates:
        if _lexes(raw[:i]):
            return raw[:i].rstrip()

    if candidates:
        try:
            shlex.split(raw)
        except ValueError:
            # Quoting neither shlex nor the head-test can resolve -- a $'...'
            # ANSI-C string, say. FAIL CLOSED and cut at the first word-initial
            # '#', so a comment can never survive here to satisfy a guard. A
            # runnable line that does not lex is broken on its own terms, and
            # test_every_runnable_line_lexes_cleanly asserts the runbook has none.
            return raw[: candidates[0]].rstrip()

    return raw.rstrip() + trailing


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


# Comment stripping is load-bearing for EVERY assertion in this file -- review
# satisfied four of them with a comment -- so it is pinned directly rather than
# only through the assertions that depend on it. Half these cases guard against
# the stripper being too EAGER: a `#` inside quotes, or inside a URL fragment,
# is not a comment, and eating it would corrupt a legitimate command and make
# this file fail for a reason that has nothing to do with deploys.
_STRIP_CASES: tuple[tuple[str, str], ...] = (
    ("echo hi  # trailing", "echo hi"),
    ("# whole line", ""),
    ("  # indented whole line", ""),
    ("curl 'https://x#frag'", "curl 'https://x#frag'"),
    ('curl "https://x#frag"', 'curl "https://x#frag"'),
    ("curl https://x#frag", "curl https://x#frag"),
    ("""curl -d '{"a":"#b"}'""", """curl -d '{"a":"#b"}'"""),
    # `$(` re-enters an unquoted context, so this IS a comment to the shell.
    ('printf "$(curl -sS  # c', 'printf "$(curl -sS'),
    ('printf "$(curl -sSL -w x)"  # c', 'printf "$(curl -sSL -w x)"'),
    # `#` preceded by a non-space is not a comment: parameter expansion.
    ("echo ${VAR#prefix}", "echo ${VAR#prefix}"),
    ("echo ${VAR##*/}", "echo ${VAR##*/}"),
    ("echo $((1#2))", "echo $((1#2))"),
    # A backslash escapes the next char inside double quotes.
    ('echo "a\\" # b"', 'echo "a\\" # b"'),
    ("echo a\tb\t# c", "echo a\tb"),
    # The two payloads that defeated the hand-written lexer. Both are a real
    # bash comment; both were kept as "runnable" and satisfied a guard.
    ('--label=say\\"hi  # --build-arg VITE_API_DEMO_KEY=x', '--label=say\\"hi'),
    ("--label=$'a\\'b'  # --build-arg VITE_API_DEMO_KEY=x", "--label=$'a\\'b'"),
    ("echo a\\ b  # c", "echo a\\ b"),
    # A continuation must survive, or _runnable_bash cannot join the command.
    ('--build-arg X="${D:?why}" \\', '--build-arg X="${D:?why}" \\'),
    # Command substitution re-enters an unquoted context, so this IS a comment
    # even though the enclosing double quote is open -- `shlex` alone says
    # otherwise, which is how the `-L` assertion was defeated.
    (
        'printf "$(curl -sS -o /dev/null  # -L -w x)" y',
        'printf "$(curl -sS -o /dev/null',
    ),
    # ...but a QUOTED `#` inside a substitution is still not a comment.
    ("printf \"$(echo 'a # b')\"", "printf \"$(echo 'a # b')\""),
)


@pytest.mark.parametrize(("line", "expected"), _STRIP_CASES, ids=lambda v: repr(v))
def test_comment_stripping_removes_comments_and_nothing_else(line: str, expected: str) -> None:
    assert _strip_comment(line) == expected


def test_every_runnable_line_lexes_cleanly() -> None:
    """Partner for `_strip_comment`'s fail-closed branch.

    That branch cuts at the first word-initial `#` when the line cannot be
    lexed -- correct for an adversarial `$'...'`, but it would also truncate a
    legitimate command with exotic quoting. This asserts the runbook contains
    no such line, so the branch only ever fires on something already broken.
    """
    unlexable = []
    for ln in _runnable_bash().splitlines():
        try:
            shlex.split(ln)
        except ValueError as exc:
            unlexable.append(f"{ln!r}: {exc}")
    assert not unlexable, (
        "docs/DEPLOY_FLY.md has runnable lines that do not lex as shell:\n  "
        + "\n  ".join(unlexable)
        + "\nThese are broken commands, and they also put _strip_comment on its "
        "fail-closed path where it truncates at the first '#'."
    )


def test_the_dockerfile_extraction_finds_the_baked_args_at_all() -> None:
    args = _baked_build_args()
    assert _frontend_stage(), "could not locate the `AS frontend` stage in Dockerfile.api"
    assert len(args) >= 2, f"expected at least 2 ARGs in the frontend stage, found {args}"
    assert "VITE_API_DEMO_KEY" in args, (
        f"VITE_API_DEMO_KEY is the argument whose absence caused #296; if it is no "
        f"longer declared in the frontend stage this guard has lost its subject. "
        f"Found: {args}"
    )


def test_every_arg_line_in_the_frontend_stage_was_parsed() -> None:
    """An ARG the regex cannot read disappears instead of failing.

    `_baked_build_args` drives a parametrised test, so a declaration it does
    not match produces no case at all -- silently one fewer guarded argument,
    with the suite green. Counting the raw `ARG` lines and requiring the parse
    to account for all of them turns "invisible" into "red".
    """
    stage = "\n".join(
        ln for ln in _frontend_stage().splitlines() if not ln.lstrip().startswith("#")
    )
    declared = len(re.findall(r"^\s*ARG\s", stage, re.M))
    parsed = len(_baked_build_args())
    assert declared and parsed == declared, (
        f"{declared} ARG lines in the frontend stage but only {parsed} parsed. An "
        f"unparsed declaration silently drops a guarded build argument."
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


def test_the_deploy_command_builds_locally() -> None:
    """#385. ``--local-only`` must stay on the §4.1 command.

    Without it ``fly deploy`` uploads the build context to Fly's REMOTE
    builder. Reported from the owner's v12 session: that builder stalled three
    times running on the upload -- 548 KB in 825 s, then ``deadline_exceeded``.
    ``--local-only`` worked first try for v12 and again for v17 on 2026-09-08.

    This is mechanical rather than prose for the same reason the rest of this
    file is: #385's own complaint is that the flag was documented NOWHERE while
    three paragraphs of the runbook discussed deploy failure modes. A note that
    a later edit can silently delete reproduces exactly that.

    It is a WEAK guard on purpose, and saying so is the point: it asserts the
    flag is on the command, not that the deploy works. What it CANNOT see is
    whether a local Docker daemon is running, whether the remote builder is
    healthy today, or whether the operator pasted the command at all.

    RED if ``--local-only`` is removed from the §4.1 deploy command. Its
    non-vacuity partner is
    ``test_the_runbook_extraction_finds_the_deploy_command_at_all`` above, which
    fails if the extraction returns nothing to check.
    """
    cmds = _deploy_commands()
    assert cmds, "no runnable `fly deploy` line found under '### 4.1'"
    missing = [c for c in cmds if "--local-only" not in c]
    assert not missing, (
        "a docs/DEPLOY_FLY.md §4.1 deploy command does not pass `--local-only`, so it "
        "uploads the build context to Fly's remote builder -- the step that stalled "
        "three times on v12 with `deadline_exceeded` (#385). A failed build creates NO "
        "release, so `fly releases` looks unchanged afterwards and reads as success.\n"
        + "\n".join(missing)
    )


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
    offenders = [o for cmd in _deploy_commands() for o in _classify(cmd)]
    assert not offenders, (
        "docs/DEPLOY_FLY.md §4.1 can pass an empty build argument:\n  "
        + "\n  ".join(offenders)
        + "\nA build-arg value must be either a plain literal (no expansion at all) or "
        'a fully guarded `"${VAR:?why this is empty}"`, so the deploy aborts instead '
        "of shipping a bundle whose baked value is the empty string (#296)."
    )


def _classify(cmd: str) -> list[str]:
    """Offending `--build-arg` values in one command.

    Three rejection rules, each a scar:
      * not a guarded expansion and not a non-empty plain literal;
      * a literal equal to a default production refuses to boot with, so
        `--build-arg VITE_API_DEMO_KEY=local-demo-key` -- release v6 written
        into the runbook -- cannot pass as a "plain literal";
      * an unparsed `--build-arg` occurrence, because an extractor that skips
        its subject reports green rather than red.
    """
    rejected = _production_rejected_defaults()
    found = _BUILD_ARG_RE.findall(cmd)
    offenders = [
        f"{name}={value}"
        for name, value in found
        if not (
            (_GUARDED_EXPANSION_RE.match(value) or _PLAIN_LITERAL_RE.match(value))
            # Normalised the way `config.py::_is_weak_secret` normalises --
            # `.strip().lower()`. Comparing raw let `LOCAL-DEMO-KEY` through:
            # a value production refuses to boot with, accepted as a "plain
            # literal". That guard was written from the same source file and
            # still disagreed with it about what counts as the same value.
            and value.strip('"').strip().lower() not in rejected
        )
    ]
    if len(found) != cmd.count("--build-arg"):
        offenders.append(
            f"{cmd.count('--build-arg') - len(found)} --build-arg occurrence(s) could not "
            f"be parsed and were therefore never checked"
        )
    return offenders


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
    # Release v6 written into the runbook as a plain literal.
    ("local-demo-key", False),
    ('"local-demo-key"', False),
    ("local-admin-key", False),
    # Case and padding: `_is_weak_secret` normalises with .strip().lower(), so
    # these are the SAME value production refuses to boot with.
    ("LOCAL-DEMO-KEY", False),
    ("Local-Demo-Key", False),
    ('"LOCAL-DEMO-KEY"', False),
    ('" local-demo-key "', False),
    # Appended after a closing quote -- never captured before, so never checked.
    ('"${DEMO_KEY:?x}"$(fly ssh console -C printenv)', False),
    ('"${DEMO_KEY:?x}"`evil`', False),
    ('"1.2.3"$(evil)', False),
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

    # EVERY matching line, not the first. `next(...)` left the decoy hiding
    # place that `_deploy_commands` was fixed to remove: an illustrative block
    # added earlier let the real calls drop both cookie flags with the suite
    # green.
    asks = [ln for ln in lines if "/messages" in ln and "curl" in ln]
    assert asks, "no runnable curl posting to /messages found in docs/DEPLOY_FLY.md"
    for ask in asks:
        assert '-b "$JAR"' in ask, (
            f"the /messages call does not SEND the session cookie (-b). Without it the "
            f"call returns 404 not_found. Line:\n{ask}"
        )

    # Selected by EXCLUDING /messages. Review showed that matching on
    # "/v1/sessions" alone fell through to the /messages line -- which contains
    # that substring and carries its own -c -- so deleting -c from the create
    # call left the suite green while no jar was ever saved.
    creates = [
        ln for ln in lines if "/v1/sessions" in ln and "/messages" not in ln and "curl" in ln
    ]
    assert creates, "no runnable curl creating a session (POST /v1/sessions) found"
    for create in creates:
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
        # A TOKEN, not a substring. `-[a-zA-Z]*L` matched any hyphenated word
        # containing a capital L, so it was a proxy for the flag rather than
        # the flag -- and this file is about guards that watch the wrong thing.
        flags = [t for t in line.split() if t.startswith("-")]
        assert any(t == "--location" or re.fullmatch(r"-[a-zA-Z]*L[a-zA-Z]*", t) for t in flags), (
            f"the citation check does not follow redirects (-L), so a healthy deploy "
            f"reports 301/308 for docs.anthropic.com and developers.openai.com and the "
            f"operator is told it failed. Line:\n{line}"
        )
