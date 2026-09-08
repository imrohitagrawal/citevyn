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
A hard-coded list of "args that matter" rots exactly like the docs it guards. So
the set is read out of the Dockerfile's frontend stage, from the ``ARG``
DECLARATIONS rather than from the ``RUN`` line's env prefix: scanning the prefix
for ``NAME="${NAME}"`` was defeated twice by a still-baked argument written
unbraced or hoisted to ``ENV``. An ``ARG`` line has one spelling; a use site has
many.

WHY COMMENTS ARE STRIPPED BEFORE EVERY ASSERTION
------------------------------------------------
A shell COMMENT satisfied three assertions here while the real mechanism was
gone -- the runbook still *said* ``-b "$JAR"`` and ``check_bundle_key.sh`` above
a command that did neither. Everything above the #388 section reads
:func:`_runnable_bash`, which deletes comment lines and joins continuations, so
an assertion can only be satisfied by something an operator would EXECUTE.

WHY `fly.toml` IS NOT ACCEPTED AS AN ALTERNATIVE HOME
------------------------------------------------------
``VITE_API_DEMO_KEY`` is a credential whose value must be read from the RUNNING
machine at deploy time, which no committed file can do. (``test_fly_config.py``
also forbids a plaintext credential in ``fly.toml``, but only in its ``[env]``
TABLE -- both of its checks read ``fly_config["env"]``, and ``build.args``
appears nowhere in it -- so that is a second reason, not the load-bearing one.)

SCOPE, STATED HONESTLY
----------------------
This proves the deploy COMMAND is complete and cannot silently pass an empty
value. It cannot prove the operator ran it, nor that the value was right -- that
is ``scripts/check_bundle_key.sh``, which inspects the SERVED bundle, and
``tests/shell/test_check_bundle_key.sh``, which tests that checker.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "infra" / "docker" / "Dockerfile.api"
_RUNBOOK = _REPO_ROOT / "docs" / "DEPLOY_FLY.md"
_CONFIG = _REPO_ROOT / "backend" / "app" / "core" / "config.py"
_TWIN = _REPO_ROOT / "backend" / "tests" / "test_backlog_table_renders.py"


def _production_rejected_defaults() -> set[str]:
    """The publicly-known defaults `Settings` names as weak in production.

    A SUBSET of what production refuses: `_is_weak_secret` also imposes a
    16-character floor, which this does not reproduce. Derived from `config.py`'s
    `_is_weak_secret(..., default="...")` calls rather than listed, because a
    hand-kept copy rots. A build arg may not be passed as a LITERAL equal to one
    of these -- `--build-arg VITE_API_DEMO_KEY=local-demo-key` is a plain literal
    with no expansion, and also the exact value that produced release v6.
    """
    return set(re.findall(r'default="(local-[a-z-]+)"', _CONFIG.read_text(encoding="utf-8")))


# Accepted `--build-arg` value shapes. This is a WHITELIST, deliberately: the
# blacklist it replaced flagged `$(...)` and an unguarded `${...}` and let a
# BACKTICK substitution and a brace-less `"$DEMO_KEY"` straight through. A guard
# that enumerates the BAD forms only catches the ones its author thought of.
#
# The value alternation ends in `\S*`, not `\S+`, so a bare
# `--build-arg VITE_API_DEMO_KEY=` is CAPTURED as an empty value and rejected.
# With `\S+` the regex did not match that form at all, so the argument was never
# examined -- the most direct spelling of the #296 outage, passing both guards.
#
# The `(?=\s|$)` after the quoted alternative is load-bearing: without it the
# capture stopped at the first closing quote, so `"${DEMO_KEY:?x}"$(fly ssh ...)`
# classified as safe on a truncated capture. Anchoring a pattern to a value you
# did not fully capture anchors nothing.
_BUILD_ARG_RE = re.compile(r'--build-arg ([A-Z][A-Z0-9_]*)=("(?:[^"\\]|\\.)*"(?=\s|$)|\S*)')

# `"${VAR:?message}"` and nothing else -- anchored to the WHOLE value, so a
# guarded expansion with anything appended (`"${VAR:?x}`evil`"`) is rejected.
# `[^{}]*` in the message keeps a nested `${OTHER:-default}` out.
_GUARDED_EXPANSION_RE = re.compile(r'^"\$\{[A-Za-z_][A-Za-z0-9_]*:\?[^{}]*\}"$')

# A NON-EMPTY literal with no expansion syntax at all: `true`, `dev`, `1.2.3`,
# optionally quoted with BALANCED quotes. The `+` and the balanced alternation
# are both load-bearing: `^"?[A-Za-z0-9_.:/-]*"?$` matched the EMPTY string, so
# `--build-arg VITE_API_DEMO_KEY=""` -- the #296 outage written verbatim -- was
# accepted as a "plain literal", and a lone `"` slipped through the same way.
_PLAIN_LITERAL_RE = re.compile(r'^(?:"[A-Za-z0-9_.:/-]+"|[A-Za-z0-9_.:/-]+)$')

# The `deploy` SUBCOMMAND of the Fly CLI, in every spelling the CLI accepts.
# ONE definition, used by BOTH halves of this file -- the §4.1 content guards and
# the repo-wide absence sweep. Two matchers over the same subject let a command
# be invisible to both at once; see `_deploy_commands` for the measured case.
#
# `fly(?:ctl)?` -- `flyctl` is the binary's real name, used at
# `docs/NEXT_SESSION_PROMPT.md:90` and in #385's own body. `\s+` rather than one
# space -- `fly  deploy` and `fly\tdeploy` are the same command to a shell. The
# repeating `(?:-\S+(?:\s+\S+)?\s+)*` admits GLOBAL FLAGS before the subcommand:
# `fly -a citevyn deploy ...` and `fly -t TOK -a app deploy ...`. Every one of
# those was missed by `\bfly deploy\b`.
#
# The ``(?<!`)`` lookbehind is GONE: in `.md` prose is already excluded by
# scanning only code blocks, and in a `.toml`/`.sh` COMMENT it was a hole,
# because a backticked command in a config comment is what a human copies the
# inside of. The `(?=\s+-)` lookahead is gone too -- it missed
# `fly deploy . --build-arg ...` -- and its job is done by `_HAS_FLAG_RE`.
_FLY_DEPLOY_COMMAND_RE = re.compile(r"\bfly(?:ctl)?\s+(?:-\S+(?:\s+\S+)?\s+)*deploy\b")

# A flag, anywhere after the subcommand: what separates a COMMAND from a MENTION
# now the lookahead is gone. `-{1,2}` accepts a SINGLE dash, because
# `fly deploy -a citevyn` is a real command and a `--`-only rule read it as a
# mention. Anchored on a word boundary so `re-embed` and `owner-gated` -- hyphens
# with no preceding space -- do not count.
_HAS_FLAG_RE = re.compile(r"(?:^|\s)-{1,2}[A-Za-z]")


def _is_deploy_command(body: str) -> bool:
    """Is ``body`` a copy-pasteable ``fly deploy`` COMMAND rather than a mention?"""
    match = _FLY_DEPLOY_COMMAND_RE.search(body)
    return match is not None and _HAS_FLAG_RE.search(body[match.end() :]) is not None


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
    # Docker, and a new baked argument slipped past the one-space form.
    return re.findall(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", _frontend_stage(), re.M)


def _runnable_bash(section: str | None = None) -> str:
    """Fenced bash the operator would actually execute, comments removed.

    Three transformations, each load-bearing: only ```` ```bash ```` fences (so
    this file can DISCUSS the check it replaced without tripping over the
    mention); blockquote markers stripped; and COMMENT LINES DELETED with
    continuations joined, so an assertion is satisfied only by a real command.

    NOT the same extractor as :func:`_copy_pasteable_lines`: this one answers
    "what would the operator RUN", that one "what could a human COPY".
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

    `shlex` answers this for plain quoting but has no concept of command
    substitution: inside `"$( ... )"` the shell re-parses, so a `#` there IS a
    comment even though the enclosing quote is open. So when the whole head does
    not lex, retry from each `$(` in turn, longest first. A `#` genuinely inside
    quotes still fails every retry.
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

    Two rules, because neither alone is faithful. WORD-INITIAL is mine: `#`
    starts a comment only at a line start or after whitespace, which `shlex` does
    not know -- it truncates `${VAR#prefix}`, `${VAR##*/}` and `$((1#2))` alike.
    UNQUOTED is `shlex`'s: a hand-written lexer got that wrong three times, so
    the question is answered by asking `shlex` whether the text BEFORE the `#`
    lexes -- an unterminated quote means the `#` is inside one.

    The raw string is cut rather than rebuilt from tokens because the value
    checks need the original text.
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

    Taking the first matching block let an illustrative block above the real one
    hide a command that dropped ``VITE_API_DEMO_KEY`` with the suite green.

    Selected with :data:`_FLY_DEPLOY_COMMAND_RE`, the SAME matcher the repo-wide
    sweep uses, not a ``"fly deploy" in ln`` substring. The substring was a hole
    this file opened itself: the sweep exempts every line of §4.1 by (path, line)
    without reading the body, so a command planted there is checked HERE or
    nowhere -- and measured, ``flyctl deploy ...``, ``fly  deploy ...`` and
    ``fly -a citevyn deploy ...`` were invisible to BOTH halves at once, with all
    108 tests green.
    """
    return _deploy_command_lines(_runnable_bash("4.1"))


def _deploy_command_lines(bash: str) -> list[str]:
    """The `fly deploy` lines of a runnable-bash block.

    Split out from :func:`_deploy_commands` so a test can drive the SELECTOR
    with text the real runbook does not contain. Inlined, the selector could
    only ever be exercised against a §4.1 that holds exactly one command written
    exactly one way, so reverting it to a substring changed nothing any
    assertion could see.
    """
    return [ln for ln in bash.splitlines() if _FLY_DEPLOY_COMMAND_RE.search(ln)]


# ---------------------------------------------------------------------------
# Partners. Every assertion below is "X is present in Y", which passes trivially
# if the extraction returns nothing, so the extractions are pinned first.
# ---------------------------------------------------------------------------


# Comment stripping is load-bearing for EVERY assertion above the #388 section --
# a comment satisfied four of them -- so it is pinned directly. Half these cases
# guard against the stripper being too EAGER: a `#` inside quotes or a URL
# fragment is not a comment, and eating it would corrupt a legitimate command.
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

    That branch cuts at the first word-initial `#` when a line cannot be lexed --
    correct for an adversarial `$'...'`, but it would also truncate a legitimate
    command with exotic quoting. This asserts the runbook has no such line.
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

    `_baked_build_args` drives a parametrised test, so a declaration it does not
    match produces no case at all. Counting the raw `ARG` lines and requiring the
    parse to account for all of them turns "invisible" into "red".
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

    Otherwise a required case could be silenced by deleting the use site and
    leaving the declaration -- or a declared-but-unused ARG would make this file
    demand a deploy flag that does nothing.
    """
    # Comments stripped before counting. The Dockerfile discusses these args at
    # length in comments, so counting raw occurrences would let a declared-but-
    # unused ARG be "used" by a sentence about it.
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


@pytest.mark.parametrize(
    "spelling",
    (
        "flyctl deploy --app citevyn --build-arg VERSION=1.2.3",
        "fly  deploy --app citevyn --build-arg VERSION=1.2.3",
        "fly\tdeploy --app citevyn --build-arg VERSION=1.2.3",
        "fly -a citevyn deploy --build-arg VERSION=1.2.3",
        "fly -t TOKEN -a citevyn deploy --build-arg VERSION=1.2.3",
        "fly deploy . --build-arg VERSION=1.2.3",
    ),
    ids=lambda v: v[:34],
)
def test_the_section_4_1_selector_sees_every_spelling_of_the_command(spelling: str) -> None:
    """§4.1 is EXEMPT from the repo-wide sweep, so this selector is the only net.

    The sweep skips every line of §4.1 by (path, line) without reading the body,
    so a stale command planted there is caught by the content guards below or by
    nothing at all. While those guards used a ``"fly deploy" in ln`` substring,
    three of the spellings below were invisible to BOTH halves at once --
    measured in the real runbook's §4.1 fence, 108 tests green.

    RED if `_deploy_commands` goes back to a substring, or if
    `_FLY_DEPLOY_COMMAND_RE` narrows.
    """
    # Driven through the SELECTOR, not through the regex alone: pointing the
    # assertion at `_FLY_DEPLOY_COMMAND_RE` would leave `_deploy_commands` free
    # to go back to a substring with every test green.
    block = f"curl -sS -o /dev/null https://citevyn.stackclimb.com/health\n{spelling}\nfly logs\n"
    assert _deploy_command_lines(block) == [spelling], (
        f"{spelling!r} is a `fly deploy` command the §4.1 selector cannot see, so a "
        f"stale copy written that way inside §4.1 is checked by nothing. Selected: "
        f"{_deploy_command_lines(block)}"
    )
    # ...and the inverse, so the selector is not simply selecting everything.
    mentions = "fly logs --app citevyn\nflying deployment tools\ndeploy the app now\n"
    assert _deploy_command_lines(mentions) == [], (
        f"the selector matched a line that is not a deploy command: "
        f"{_deploy_command_lines(mentions)}"
    )


def test_the_extraction_is_scoped_to_section_4_1() -> None:
    """The rollback command in §6.1 must not be what we are reading.

    Without this, a future edit broadening the section regex would start matching
    ``fly deploy --image``, which passes no build args. The `--app` half is
    asserted on the deploy LINE: a whole-block check was satisfied by the
    `fly ssh console --app citevyn` line above it.
    """
    cmds = _deploy_commands()
    assert not any("--image" in c for c in cmds), (
        "§4.1 extraction picked up the §6.1 rollback command (`fly deploy --image`)"
    )
    # Asserted on the deploy LINE. A whole-block check was satisfied by the
    # `fly ssh console --app citevyn` line above it, so removing `--app` from
    # the deploy command alone stayed green.
    assert all("--app citevyn" in c for c in cmds), f"a §4.1 deploy command has no --app: {cmds}"


def test_the_deploy_command_builds_locally() -> None:
    """#385. ``--local-only`` must stay on the §4.1 command.

    Without it ``fly deploy`` uploads the build context to Fly's REMOTE builder,
    which stalled three times on v12 -- 548 KB in 825 s, then
    ``deadline_exceeded``. ``--local-only`` worked first try for v12 and again
    for v17 on 2026-09-08.

    A WEAK guard on purpose: it asserts the flag is on the command, not that the
    deploy works. It cannot see whether a local Docker daemon is running, whether
    the remote builder is healthy, or whether the operator pasted the command.

    RED if ``--local-only`` is removed from the §4.1 deploy command.
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
    """A prose mention does not count -- it must be an actual `--build-arg`.

    Matched as `--build-arg[= ]NAME=`, both spellings. The literal
    `--build-arg NAME=` search this used to do disagreed with `_classify`, which
    captures the equals form too, so one guard checked a command the other could
    not see. (Whether `fly` accepts the equals form is UNVERIFIED -- one
    `fly deploy --help` settles it, and this session must not run `fly`.)
    """
    cmds = _deploy_commands()
    arg_re = re.compile(rf"--build-arg[= ]{re.escape(arg)}=")
    missing = [c for c in cmds if not arg_re.search(c)]
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

    The second half of #296, and the subtler one. The runbook used to read the key
    with an inline `$(fly ssh console ...)`; when the machine has scaled to zero
    that yields an EMPTY STRING, and -- measured with docker --
    `--build-arg NAME=""` does not fall back to the ARG default. Vite bakes `""`,
    the browser sends a bare `Authorization: Bearer `, and production 401s as in
    v6. `${VAR:?msg}` exits the shell before `fly deploy` runs, in bash and zsh.
    """
    cmds = _deploy_commands()
    # The non-vacuity partner every sibling in this file has and this one did
    # not: `assert not offenders` is loudest when there was nothing to classify.
    assert cmds, "no runnable `fly deploy` line found under '### 4.1'"
    offenders = [o for cmd in cmds for o in _classify(cmd)]
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


# Every value shape ever proposed for this argument, and whether the classifier
# must reject it. The test above only ever sees the ONE shape the runbook uses,
# so a hole in the regex would be invisible until someone wrote that form into
# the docs -- precisely too late. Three entries are scars: `"$DEMO_KEY"` and the
# backtick form each walked through the original blacklist, and `""` walked
# through the deny-by-default rewrite that replaced it.
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
    contains neither the default nor any key, so that check prints 0 and reports
    success on precisely the outage it was added to catch. Both halves below are
    structural rather than substring, because both were defeated: quoting the
    pattern walked past a literal search, and demoting the replacement to a
    COMMENT satisfied its presence check while nothing ran it.
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

    # Selected by EXCLUDING /messages. Matching on "/v1/sessions" alone fell
    # through to the /messages line -- which contains that substring and carries
    # its own -c -- so deleting -c from the create call left the suite green
    # while no jar was ever saved.
    creates = [
        ln for ln in lines if "/v1/sessions" in ln and "/messages" not in ln and "curl" in ln
    ]
    assert creates, "no runnable curl creating a session (POST /v1/sessions) found"
    for create in creates:
        assert '-c "$JAR"' in create, (
            f"the POST /v1/sessions call does not SAVE a cookie jar (-c), so there is "
            f"nothing for the /messages call to send back. Line:\n{create}"
        )

    # The bearer must come from a variable the runbook actually sets. §4.4 used
    # $CITEVYN_DEMO_API_KEY, which the runbook never assigns in the operator's
    # shell, so the bearer was empty and /v1/sessions 401'd. Anchored at COLUMN 0
    # and excluding continuation lines, because §3's block writes
    # `  CITEVYN_DEMO_API_KEY=... \` as an indented ARGUMENT to `fly secrets set`
    # -- a Fly secret, not a shell variable -- and matching the whole document
    # accepted it.
    assigned = "\n".join(
        ln for ln in _runnable_bash().splitlines() if not ln.rstrip().endswith("\\")
    )
    # EVERY ask and EVERY create, not the leaked loop variables of the two loops
    # above: `for line in (ask, create):` re-used Python's leaked bindings and ran
    # against only the LAST of each. LATENT TODAY -- the runbook holds one ask and
    # one create -- so it was BITE-PROVED with a planted decoy carrying
    # `Bearer $NEVER_ASSIGNED_ANYWHERE`, which reddens this loop and leaves the
    # leaked-variable version green. The runbook was restored byte-identically.
    for line in [*asks, *creates]:
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
    `%{http_code}` tells the operator a healthy deploy has failed.
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


# ---------------------------------------------------------------------------
# #388: ONE copy of the deploy command, and a guard that says so.
#
# The two stale copies this replaced -- `docs/RELEASE_CANDIDATE_v0.12.0.md` and
# the `[build.args]` comment in `fly.toml` -- had each drifted TWO revisions
# behind, independently, and both still read
# `fly deploy --build-arg VERSION=$(...)`: they had missed #296 (2026-09-02,
# VITE_API_DEMO_KEY, a ~1h outage) and #385 (2026-09-08, --local-only). That one
# line is defective four ways at once (an unguarded `$(...)`, no `--app`, no
# `--local-only`, neither VITE_* argument) and FOUR DIFFERENT guards in this file
# find them, not one: `_classify` yields exactly one offender on that line (the
# `$()`), while `--app`, `--local-only` and the VITE_* arguments belong to
# `test_the_extraction_is_scoped_to_section_4_1`,
# `test_the_deploy_command_builds_locally` and `_baked_build_args()`.
#
# WHY DELETE-AND-LINK RATHER THAN A THREE-WAY SYNC GUARD. Keeping three copies
# byte-compatible across three comment syntaxes leaves the drift mechanism in
# place and only makes it noisier; deleting them removes it. `fly.toml` cannot
# hold the correct command even in principle -- it needs `${DEMO_KEY:?...}` read
# from the RUNNING MACHINE at deploy time -- so any copy there is a partial one.
# And an ABSENCE guard cannot be satisfied by its own subject, while a presence
# guard is satisfied by the literal string it greps for.
#
# THE COMMENT INVERSION. Everything above STRIPS comments, so only text an
# operator would EXECUTE can satisfy an assertion. Here the subject is the
# opposite: in a `.toml` or `.sh` file the COMMENT is what a human copies, and in
# a `.md` file a code block is copied whether or not its lines start with `#`. So
# this scanner reads comments deliberately and reuses neither `_runnable_bash`
# nor `_strip_comment`. `test_fly_config.py` reads `fly.toml` through `tomllib`,
# which DISCARDS comments, so no existing test could see that line at all.
#
# INVISIBLE BY CONSTRUCTION, with no allowlist entry to rot: prose anywhere
# (`CHANGELOG.md`, `docs/NEXT_SESSION_PROMPT.md`, `docs/BACKLOG.md` table cells,
# `Dockerfile.api`'s comment) is neither a markdown code block nor a toml/sh
# comment line, and a `.py` file is neither -- which is why the
# `fly deploy --app citevyn --build-arg ...` string in THIS file needs no
# exemption.
# ---------------------------------------------------------------------------

_SCANNED_SUFFIXES = frozenset({".md", ".toml", ".sh"})

# Same prune list as `test_node_version_pin.py`. Working tree rather than
# `git ls-files` for the same reason it gives: an untracked copy of the command
# is just as copy-pasteable. The cost is that a scratch file on a laptop can
# fail this locally; CI checks out clean, so it cannot fire there.
_PRUNED_DIRS = frozenset(
    {
        ".git",
        ".claude",
        "node_modules",
        "dist",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
    }
)

# `--build-arg NAME=` OR `--build-arg=NAME=`. The equals form is handled
# defensively: `_classify` above already captures it, while its sibling
# `test_every_baked_build_arg_is_passed_by_the_deploy_command` searched for the
# literal `--build-arg NAME=` and did not, so the two disagreed about the same
# command. Whether `fly` itself accepts `--build-arg=NAME=value` is UNVERIFIED
# -- settling it costs one `fly deploy --help`, which this session must not run
# -- so both spellings are treated as passing a build argument.
_BUILD_ARG_ANY_RE = re.compile(r"--build-arg[= ]")


# --- BEGIN SHARED CODE-BLOCK TRACKER ---------------------------------------
# Byte-identical in `test_backlog_table_renders.py` and
# `test_deploy_fly_build_args_are_documented.py`, and the equality is ASSERTED,
# not merely asked for in a comment: see
# `test_the_shared_code_block_tracker_has_not_drifted` in the deploy file, which
# compares the two blocks byte for byte. They are duplicated rather than
# imported so the two guards can still fail independently.


def _physical_lines(text: str) -> list[str]:
    """``text`` split on ``\\n`` only, with a trailing ``\\r`` removed.

    NOT ``str.splitlines()``, which also splits on U+0085, U+2028, U+2029,
    ``\\x0b`` and ``\\x0c``. Markdown does not: measured, a table cell holding any
    of those renders as one row with every cell intact, while `splitlines` tore
    the row in two and shifted every line number after it. Splitting on ``\\n``
    alone is what makes the 1:1 line numbering both guards report true.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


# A fence opener: up to three spaces of indent, then three or more backticks or
# tildes. Group 3 is the info string, which for a BACKTICK fence may not itself
# contain a backtick (CommonMark 4.5) -- that rule is what keeps a line of
# inline code from being read as a fence.
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

# FOUR or more spaces of indent opens an INDENTED CODE BLOCK (CommonMark 4.4),
# but only after a blank line: an indented line interrupting a paragraph is a
# lazy continuation, not code. Its content is CODE in both files, and the two do
# OPPOSITE things with it -- which is why the DETECTOR is shared and the reaction
# is not. The BACKLOG guard stops reading it as a table (measured: an indented
# row renders as `<pre>`); the deploy guard starts reading it as copy-pasteable
# (`docs/DEMO_CHECKLIST.md:51-54` is a live indented `psql` command).
#
# A TAB is not handled: CommonMark expands one to four spaces, this does not.
_INDENTED_CODE_RE = re.compile(r"^ {4,}\S")


def _opens_fence(match: re.Match[str]) -> bool:
    return not (match.group(2)[0] == "`" and "`" in match.group(3))


def _closes_fence(match: re.Match[str], char: str, length: int) -> bool:
    return (
        match.group(2)[0] == char and len(match.group(2)) >= length and not match.group(3).strip()
    )


def _code_mask(lines: list[str]) -> list[str]:
    """``""``, ``"marker"`` or ``"code"`` for each line of ``lines``.

    FLAVOUR-AWARE. A naive toggle over ``^\\s*(?:```|~~~)`` lets a ``~~~`` line
    INSIDE a ```` ```python ```` block close it early and inverts the parity of
    every fence after it. A fence closes only on the same marker character, at
    least as long as the opener, with nothing but whitespace after it.
    """
    mask: list[str] = []
    fence: tuple[str, int] | None = None
    indented = False
    prev_blank = True
    for line in lines:
        blank = not line.strip()
        if fence is None and not indented and prev_blank and _INDENTED_CODE_RE.match(line):
            indented = True
        if indented:
            if blank or _INDENTED_CODE_RE.match(line):
                mask.append("code")
                prev_blank = blank
                continue
            indented = False
        match = _FENCE_RE.match(line)
        if fence is None:
            if match is not None and _opens_fence(match):
                fence = (match.group(2)[0], len(match.group(2)))
                mask.append("marker")
            else:
                mask.append("")
        elif match is not None and _closes_fence(match, *fence):
            fence = None
            mask.append("marker")
        else:
            mask.append("code")
        prev_blank = blank
    return mask


# (name, markdown template, is the `{p}` payload inside a code block?). Both
# files parametrise their own consumer of `_code_mask` over this table with their
# own payload.
_CODE_BLOCK_SHAPES: tuple[tuple[str, str, bool], ...] = (
    ("backtick fence", "```\n{p}\n```\n", True),
    ("tilde fence", "~~~\n{p}\n~~~\n", True),
    ("fence with an info string", "```markdown\n{p}\n```\n", True),
    ("fence indented by three spaces", "   ```\n{p}\n   ```\n", True),
    ("tilde line inside a backtick fence", "```python\n~~~~\n{p}\n```\n", True),
    ("backtick line inside a tilde fence", "~~~python\n```\n{p}\n~~~\n", True),
    # A longer opener needs a closer at least as long...
    ("four-backtick fence closed by three", "````\n```\n{p}\n````\n", True),
    # ...and a LONGER closer does close a shorter opener, so this payload is
    # outside the block. Measured: GitHub renders the table that follows.
    ("three-backtick fence closed by four", "```\ncode\n````\n\n{p}\n\n", False),
    # A closing fence carries NO info string, so a same-flavour line that has one
    # does not close the block.
    ("info-string line inside a fence", "```\n```python\n{p}\n```\n", True),
    ("four-space indented code block", "text\n\n    {p}\n\ntail\n", True),
    ("fence indented by four spaces", "text\n\n    ```\n    {p}\n    ```\n\ntail\n", True),
    # An indented line that INTERRUPTS a paragraph is a lazy continuation, not
    # code, so this payload is live text.
    ("four-space indent with no blank line above", "text\n    {p}\n\ntail\n", False),
    # Not a fence at all: a backtick fence's info string may not contain a
    # backtick, so this is inline code. THREE backticks on purpose -- with two,
    # `_FENCE_RE` never matches and the rule under test is never reached.
    ("inline code, not a fence", "```a | b```\n\n{p}\n\n", False),
    ("no fence at all", "prose\n\n{p}\n\n", False),
)
# --- END SHARED CODE-BLOCK TRACKER -----------------------------------------


# (path relative to the repo root, the exact TEXT exempted, why it is allowed).
#
# LINE-SCOPED VIA THE ANCHOR, not file-scoped: with a file-scoped allowlist,
# appending a genuinely stale `fly deploy --build-arg VERSION=$(git describe ...)`
# to `scripts/check_bundle_key.sh` produced `offenders = []`, because every copy
# in an allowlisted file rode the one entry.
#
# Every entry is asserted to STILL MATCH something by
# `test_every_allowlisted_deploy_copy_still_exists`, or an exemption outlives the
# line it exempts and the allowlist grows until the guard covers nothing.
_ALLOWED_COPIES: tuple[tuple[str, str, str], ...] = (
    (
        "scripts/check_bundle_key.sh",
        'fly deploy --app citevyn --build-arg VITE_API_DEMO_KEY=\\"\\${DEMO_KEY:?}\\" ...',
        "an incident-remediation HINT printed to stderr when the SERVED bundle "
        "carries the wrong key. It deliberately ends in `...` rather than being "
        "runnable, and the line after it sends the operator to DEPLOY_FLY.md "
        "§4.1 for the real command -- so it cannot go stale in the way #388 is "
        "about, because it was never complete.",
    ),
)


def _scanned_files(root: Path) -> list[Path]:
    """Every `.md`, `.toml` and `.sh` file under ``root``.

    ``onerror`` RAISES rather than using ``os.walk``'s default of ``None``, which
    DISCARDS the ``OSError`` -- an unreadable directory would vanish from the
    scan and the absence gate would report clean.

    Takes a ``root`` so a test can drive the real sweep over a tree that has a
    planted offender; the repo has zero.
    """

    def _raise(exc: OSError) -> None:
        raise exc

    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
        found.extend(
            Path(dirpath) / name
            for name in filenames
            if Path(name).suffix.lower() in _SCANNED_SUFFIXES
        )
    return sorted(found)


def _join_continuations(pairs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Join backslash-continued lines, keeping the FIRST line's number.

    Without this a command split over four lines -- exactly how §4.1 writes it --
    is invisible to a line-oriented scan. Only ADJACENT scan units are joined, so
    a continuation running out of a code block cannot manufacture a command that
    neither line contains.
    """
    joined: list[tuple[int, str]] = []
    start: int | None = None
    buf = ""
    last = 0
    for line_no, text in pairs:
        body = text.rstrip()
        continues = body.endswith("\\") and not body.endswith("\\\\")
        if continues:
            body = body[:-1].rstrip()
        if start is None:
            start, buf, last = line_no, body, line_no
        elif line_no == last + 1:
            buf, last = f"{buf} {body.strip()}", line_no
        else:
            joined.append((start, buf))
            start, buf, last = line_no, body, line_no
        if not continues:
            joined.append((start, buf))
            start, buf = None, ""
    if start is not None:
        joined.append((start, buf))
    return joined


def _copy_pasteable_lines(suffix: str, text: str) -> list[tuple[int, str]]:
    """``(line no, text)`` for the lines of `text` a human would copy and run.

    Per file kind, because "copy-pasteable" means something different in each:

    * ``.md`` -- only inside a CODE BLOCK, fenced or four-space indented, with
      blockquote markers stripped (§4.1 nests code inside ``>``) and comment
      lines KEPT, because a commented-out command in a runbook fence is copied
      and uncommented. Everything outside a code block is prose, so table cells
      and CHANGELOG sentences are excluded with no allowlist entry. Indented
      blocks were a stated blind spot justified as "a shape this repo does not
      use"; measured, FALSE -- ``docs/DEMO_CHECKLIST.md:51-54`` is a
      six-space-indented fence around a live ``psql`` command. The cost, stated:
      an indented list continuation after a blank line reads as code too.
    * ``.toml`` -- only ``#`` comment lines: nothing else there is a command.
    * ``.sh`` -- every line, comments included.

    The ``#`` strip is PRESENTATIONAL, and an earlier docstring presented it as
    load-bearing. `_FLY_DEPLOY_COMMAND_RE` matches straight through a leading
    ``#``, so deleting it changes only how the offender message reads.
    """
    raw: list[tuple[int, str]] = []
    if suffix == ".md":
        lines = [re.sub(r"^\s*> ?", "", line) for line in _physical_lines(text)]
        raw = [
            (line_no, re.sub(r"^(\s*)#+ ?", r"\1", line))
            for line_no, (kind, line) in enumerate(
                zip(_code_mask(lines), lines, strict=True), start=1
            )
            if kind == "code"
        ]
    elif suffix == ".toml":
        for line_no, line in enumerate(_physical_lines(text), start=1):
            if re.match(r"^\s*#", line):
                raw.append((line_no, re.sub(r"^(\s*)#+ ?", r"\1", line)))
    elif suffix == ".sh":
        for line_no, line in enumerate(_physical_lines(text), start=1):
            raw.append((line_no, re.sub(r"^(\s*)#+ ?", r"\1", line)))
    return _join_continuations(raw)


def _deploy_copies(suffix: str, text: str) -> list[tuple[int, str]]:
    """Copy-pasteable `fly deploy <flags>` commands in one file's text."""
    return [
        (line_no, body)
        for line_no, body in _copy_pasteable_lines(suffix, text)
        if _is_deploy_command(body)
    ]


def _unsanctioned_in(suffix: str, text: str) -> list[tuple[int, str]]:
    """The SHAPE half of the rule: copies minus the `--image` rollback exemption.

    Split out from :func:`_unsanctioned_deploy_copies`, which additionally
    applies the two PATH-scoped exemptions, so the fixture table below drives
    exactly the rule the repo scan runs.

    The ``--image`` exemption is CONDITIONAL on passing no build argument, in
    both spellings, and is keyed on the FLAG rather than the word: a deploy
    command whose comment mentions "the image" is not a rollback.
    """
    return [
        (line_no, body)
        for line_no, body in _deploy_copies(suffix, text)
        if not ("--image" in body and not _BUILD_ARG_ANY_RE.search(body))
    ]


def _section_line_range(text: str, section: str) -> tuple[int, int]:
    """Inclusive ``(first, last)`` line numbers of a ``### <section>`` block.

    FAILS CLOSED when the section is not found: ``(0, -1)``, an empty range no
    line number can fall inside. The previous version returned
    ``(start or 0, len(lines))``, so renaming ``### 4.1`` made EVERY line of the
    runbook sanctioned -- measured.
    """
    lines = _physical_lines(text)
    start: int | None = None
    for line_no, line in enumerate(lines, start=1):
        if start is None:
            if re.match(rf"^### {re.escape(section)}(\s|$)", line):
                start = line_no
            continue
        if re.match(r"^#{2,3} ", line):
            return (start, line_no - 1)
    if start is None:
        return (0, -1)
    return (start, len(lines))


def _sanctioned_range() -> tuple[int, int]:
    return _section_line_range(_RUNBOOK.read_text(encoding="utf-8"), "4.1")


def _is_sanctioned(rel: str, line_no: int, body: str) -> bool:
    """Is a copy at ``rel``:``line_no`` reading ``body`` allowed to exist?

    The two PATH-scoped exemptions as a PURE function, so the fixture test can
    drive them: inlined into the sweep, making the allowlist unconditional left
    every test green, because an absence assertion cannot see a widened exemption
    while the repo it scans has zero offenders.

    ``body`` is the third parameter because a FILE-scoped allowlist said yes for
    line 1, line 150 and line 99999 of ``scripts/check_bundle_key.sh`` alike. The
    anchor must also be the ONLY deploy command on the line -- ``anchor in body``
    alone left the same hole one line narrower.
    """
    if rel == "docs/DEPLOY_FLY.md":
        lo, hi = _sanctioned_range()
        if lo <= line_no <= hi:
            return True
    for allowed_rel, anchor, _reason in _ALLOWED_COPIES:
        if rel == allowed_rel and anchor in body and len(_FLY_DEPLOY_COMMAND_RE.findall(body)) == 1:
            return True
    return False


def _unsanctioned_deploy_copies() -> list[str]:
    """Every copy-pasteable `fly deploy` command that is NOT the one true copy.

    Three ways to pass, and only three: it is inside ``docs/DEPLOY_FLY.md`` §4.1;
    it is a ``fly deploy --image`` rollback passing NO build argument, so it
    cannot carry the #296/#385 drift; or its file is in ``_ALLOWED_COPIES`` with
    a written reason.

    BITE-PROVED both ways: re-adding `fly deploy --build-arg VERSION=$(...)` to
    ``fly.toml``'s ``[build.args]`` comment and, separately, to
    ``docs/RELEASE_CANDIDATE_v0.12.0.md`` each turned this red.
    """
    return _sweep(_REPO_ROOT)[0]


def _sweep(root: Path) -> tuple[list[str], list[str], list[str]]:
    """``(offenders, paths PROCESSED, copies SANCTIONED)`` under ``root``.

    Three return values because an absence gate over a clean repo produces no
    positive observation, and every hollowing mutation hides in that silence.
    ``processed`` joins the rule to the enumeration (restricting the sweep to the
    one file that is exempt anyway left all 81 tests green); ``sanctioned`` is
    the only thing the REAL repo can show for itself, so a sweep short-circuiting
    on ``root == _REPO_ROOT`` has nothing to report and goes red.
    """
    offenders: list[str] = []
    processed: list[str] = []
    sanctioned: list[str] = []
    for path in _scanned_files(root):
        rel = path.relative_to(root).as_posix()
        processed.append(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            # FLAGGED, not skipped. A single non-UTF-8 byte used to make a file
            # permanently invisible with no signal at all -- "could not be read,
            # therefore not checked" is an offence, the same way `_classify`
            # treats an unparsed `--build-arg`.
            offenders.append(
                f"{rel}: could not be read as UTF-8 ({exc.__class__.__name__}), so it was "
                f"never checked for a stale deploy command. Make it UTF-8 or add it to "
                f"_PRUNED_DIRS with a reason."
            )
            continue
        for line_no, body in _unsanctioned_in(path.suffix.lower(), text):
            where = f"{rel}:{line_no}: {body.strip()}"
            if _is_sanctioned(rel, line_no, body):
                sanctioned.append(where)
            else:
                offenders.append(where)
    return offenders, processed, sanctioned


def test_the_deploy_copy_scan_reaches_the_whole_repo() -> None:
    """Partner. `assert not offenders` passes loudest when nothing was scanned.

    Measured at `5e383a1`: 74 `.md` + 2 `.toml` + 33 `.sh` = 109 files. The floor
    is 60 rather than 109 because docs come and go, but it is far enough above
    zero that a broken walk or a prune list that ate `docs/` cannot pass. The
    required list names one file of EACH scanned suffix, two of them under
    `scripts/` and `infra/`: dropping `.sh` from `_SCANNED_SUFFIXES`, or pruning
    those directories, each survived a list naming only `docs/` and `fly.toml`.
    """
    files = _scanned_files(_REPO_ROOT)
    assert len(files) >= 60, f"only {len(files)} scannable files found under {_REPO_ROOT}"
    rels = {p.relative_to(_REPO_ROOT).as_posix() for p in files}
    for required in (
        "docs/DEPLOY_FLY.md",
        "fly.toml",
        "docs/RELEASE_CANDIDATE_v0.12.0.md",
        "scripts/check_bundle_key.sh",
        "infra/docker/scripts/deploy_verify.sh",
    ):
        assert required in rels, f"{required} was not reached by the scan"


def test_the_sweep_processes_every_file_the_enumeration_returns() -> None:
    """The join between the RULE and the ENUMERATION, which nothing used to make.

    One partner proves the enumeration is large and the fixture table proves the
    shape rule is right; neither proved the sweep RUNS the rule over the
    enumeration, so restricting it to `docs/DEPLOY_FLY.md` (exempt anyway)
    reported the repo perfectly clean with all 81 tests green.

    RED if `_sweep` iterates over anything narrower than `_scanned_files`.
    """
    _offenders, processed, _sanctioned = _sweep(_REPO_ROOT)
    enumerated = [p.relative_to(_REPO_ROOT).as_posix() for p in _scanned_files(_REPO_ROOT)]
    assert sorted(processed) == sorted(enumerated), (
        f"the sweep processed {len(processed)} files but the enumeration returned "
        f"{len(enumerated)}; unprocessed: "
        f"{sorted(set(enumerated) - set(processed))[:10]}"
    )
    assert len(processed) == len(set(processed)), "the sweep processed a file twice"


def test_the_real_repo_sweep_actually_reads_the_runbook() -> None:
    """The one POSITIVE observation a clean repo can make about the sweep.

    Every other assertion over `_REPO_ROOT` is an absence, which is exactly what
    a sweep that quietly does nothing produces. Measured: a root-keyed early
    return gives the correct offenders AND the correct processed list, so it
    survived every check there was. It cannot fake this one.

    RED if `_sweep` short-circuits on the real root, if `_copy_pasteable_lines`
    stops reading `.md` code blocks, or if the §4.1 exemption stops applying.
    """
    _offenders, _processed, sanctioned = _sweep(_REPO_ROOT)
    runbook = [s for s in sanctioned if s.startswith("docs/DEPLOY_FLY.md:")]
    assert runbook, (
        f"the sweep reported no sanctioned copy in docs/DEPLOY_FLY.md, so it never "
        f"actually read §4.1's command. Sanctioned: {sanctioned}"
    )
    assert any("--local-only" in s and _BUILD_ARG_ANY_RE.search(s) for s in runbook), (
        f"the sanctioned §4.1 copy does not look like the canonical command -- the "
        f"continuations were not joined, or the wrong line was matched: {runbook}"
    )
    lo, hi = _sanctioned_range()
    for entry in runbook:
        line_no = int(entry.split(":")[1])
        assert lo <= line_no <= hi, f"{entry} is not inside the §4.1 window ({lo}-{hi})"


def test_the_sweep_finds_a_planted_offender_of_every_scanned_kind(tmp_path: Path) -> None:
    """Drive the REAL sweep over a synthetic root, one plant per suffix.

    The repo has zero offenders, so `assert not offenders` over it cannot tell a
    working sweep from a broken one.

    RED if the sweep stops reading any suffix, stops descending into `scripts/`,
    `infra/` or `docs/`, or stops applying `_unsanctioned_in`.
    """
    stale = "fly deploy --app citevyn --build-arg VERSION=1.2.3"
    plants = {
        "docs/FAKE_RUNBOOK.md": f"text\n\n```bash\n{stale}\n```\n",
        "scripts/fake.sh": f"#!/usr/bin/env bash\n{stale}\n",
        "infra/fake.toml": f"[build]\n#   {stale}\n",
    }
    for rel, body in plants.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    # ...and one file that must NOT be flagged, so this is not "flags everything".
    (tmp_path / "docs" / "CLEAN.md").write_text(
        "Prose about `fly deploy` that is not in a code block.\n", encoding="utf-8"
    )

    offenders, processed, _sanctioned = _sweep(tmp_path)
    assert sorted(processed) == sorted([*plants, "docs/CLEAN.md"]), (
        f"the sweep did not process the planted tree: {sorted(processed)}"
    )
    flagged = {o.split(":", 1)[0] for o in offenders}
    assert flagged == set(plants), (
        f"want exactly the three planted offenders, got {sorted(flagged)}: {offenders}"
    )


def test_the_sweep_finds_a_planted_offender_in_a_COPY_of_the_real_tree(tmp_path: Path) -> None:
    """The synthetic tree has four files; the real one has over a hundred.

    A sweep that stops after the first N files passes the four-file fixture and
    both absence assertions alike -- measured, `if len(processed) > 50: continue`
    survived the whole suite. This copies every file the real enumeration
    returns, plants one stale command, and requires exactly that one back.

    RED if `_sweep` stops short of the whole enumeration, or stops flagging.
    """
    for path in _scanned_files(_REPO_ROOT):
        rel = path.relative_to(_REPO_ROOT)
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, tmp_path / rel)
    planted = tmp_path / "docs" / "ZZ_PLANTED_RUNBOOK.md"
    planted.write_text(
        "text\n\n```bash\nfly deploy --app citevyn --build-arg VERSION=1.2.3\n```\n",
        encoding="utf-8",
    )

    offenders, processed, sanctioned = _sweep(tmp_path)
    assert len(processed) > 100, f"the copy is not the size of the real tree: {len(processed)}"
    assert offenders == [
        "docs/ZZ_PLANTED_RUNBOOK.md:4: fly deploy --app citevyn --build-arg VERSION=1.2.3"
    ], f"want exactly the planted offender in a copy of the real tree, got {offenders}"
    # ...and the copy still contains the real §4.1 command, sanctioned as it is
    # in the repo, so "no offenders elsewhere" is a verdict and not a blank.
    assert any(s.startswith("docs/DEPLOY_FLY.md:") for s in sanctioned), (
        f"the copied runbook's §4.1 command was not seen: {sanctioned}"
    )


def test_a_file_that_cannot_be_decoded_is_reported_rather_than_skipped(
    tmp_path: Path,
) -> None:
    """A non-UTF-8 byte must not make a file invisible to this guard.

    Reproduced against the old `except UnicodeDecodeError: continue`: a latin-1
    `.md` carrying a fenced deploy command was skipped with no signal, while
    `_unsanctioned_in` on its decoded text returned the offender.

    RED if the sweep goes back to `continue`-ing on a decode failure.
    """
    body = "café\n\n```bash\nfly deploy --app citevyn --build-arg V=1\n```\n"
    (tmp_path / "notes.md").write_bytes(body.encode("latin-1"))
    offenders, processed, _sanctioned = _sweep(tmp_path)
    assert processed == ["notes.md"], f"the file was not even enumerated: {processed}"
    assert len(offenders) == 1 and "could not be read" in offenders[0], (
        f"an undecodable file must be reported, not skipped: {offenders}"
    )
    # ...and the partner: the file really did contain something worth catching.
    assert _unsanctioned_in(".md", body), "the fixture must carry a real offender"


def test_the_deploy_copy_scanner_finds_the_canonical_command() -> None:
    """Partner. Proves the matcher matches ANYTHING at all.

    Without it, a `_FLY_DEPLOY_COMMAND_RE` that matched nothing would report the
    repo perfectly clean -- the "absence assertion satisfied by a broken scanner"
    shape this file already carries scars from.
    """
    text = _RUNBOOK.read_text(encoding="utf-8")
    lo, hi = _sanctioned_range()
    assert lo and hi > lo, f"could not locate '### 4.1' in {_RUNBOOK}: got ({lo}, {hi})"
    found = _deploy_copies(".md", text)
    in_section = [(n, b) for n, b in found if lo <= n <= hi]
    assert in_section, (
        f"the scanner found no `fly deploy` command inside DEPLOY_FLY.md §4.1 "
        f"(lines {lo}-{hi}); it found {found}. The matcher is broken, so the "
        f"absence check below is asserting nothing."
    )
    # It must have joined the continuations, or the flags that make this the
    # correct command are on lines the scan never saw.
    assert any("--local-only" in b and _BUILD_ARG_ANY_RE.search(b) for _, b in in_section), (
        f"continuations were not joined -- §4.1's command reads as {in_section}"
    )


def test_continuations_are_joined_only_across_ADJACENT_lines() -> None:
    r"""A `\` at the end of one code block must not swallow the next one.

    Neither half below is a deploy command on its own, so joining two
    non-adjacent scan units manufactures an offender out of two innocent lines.

    RED if `_join_continuations` drops its `line_no == last + 1` adjacency test.
    """
    doc = "```bash\nfly deploy \\\n```\n\ntext\n\n```bash\n--build-arg V=1\n```\n"
    assert _unsanctioned_in(".md", doc) == [], (
        f"a continuation ran out of its code block and joined an unrelated line: "
        f"{_unsanctioned_in('.md', doc)}"
    )
    # ...and the partner: ADJACENT continuation lines really are still joined.
    adjacent = "```bash\nfly deploy \\\n  --build-arg V=1\n```\n"
    assert _unsanctioned_in(".md", adjacent), "an adjacent continuation was not joined"


def test_the_sanctioned_window_is_scoped_to_section_4_1() -> None:
    """The one exempt WINDOW must stay a window, not the whole runbook.

    Found by mutation: widening `_sanctioned_range()` to `(1, 10**9)` left every
    test green, because DEPLOY_FLY.md's only other candidate is the §6.1
    rollback, already exempt on its shape. The exemption would silently cover the
    next stale copy added anywhere in the runbook.

    RED if `_sanctioned_range` is widened past the section it names.
    """
    text = _RUNBOOK.read_text(encoding="utf-8")
    total = len(_physical_lines(text))
    lo, hi = _sanctioned_range()
    assert 1 < lo <= hi < total, (
        f"the sanctioned window is ({lo}, {hi}) over a {total}-line file -- that is "
        f"not a section, it is the document."
    )
    rollbacks = [n for n, body in _deploy_copies(".md", text) if "--image" in body]
    assert rollbacks, (
        "DEPLOY_FLY.md no longer contains a `fly deploy --image` rollback, so this "
        "test's subject is gone and it is asserting nothing."
    )
    assert all(not (lo <= n <= hi) for n in rollbacks), (
        f"§6.1's rollback (lines {rollbacks}) falls inside the §4.1 window "
        f"({lo}-{hi}), so the window has swallowed another section."
    )


def test_the_path_exemptions_exempt_only_what_they_name() -> None:
    """`_is_sanctioned` must say NO to a path nobody has exempted.

    Driven with synthetic paths because the repo has zero offenders, so the
    absence gate passes identically whether the exemptions are narrow or
    universal -- `if rel in allowed:` -> `if True:` left all 80 tests green.

    THE EXPECTATIONS ARE HARD-CODED, and the previous version's were not: it
    computed `lo, hi = _sanctioned_range()` and asserted about `lo + 1`, so
    mutating that function to `(1, 10**9)` left both assertions holding.

    RED if either exemption stops being keyed on the path, the line range, or the
    anchor text it names.
    """
    # The §4.1 window: located INDEPENDENTLY, by finding the canonical command
    # itself rather than by asking the function under test where §4.1 is.
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    canonical = [
        line_no
        for line_no, body in _deploy_copies(".md", runbook)
        if "--local-only" in body and _BUILD_ARG_ANY_RE.search(body)
    ]
    assert len(canonical) == 1, f"want exactly one canonical §4.1 command, found {canonical}"
    assert _is_sanctioned("docs/DEPLOY_FLY.md", canonical[0], "irrelevant"), (
        "the §4.1 window must exempt the one canonical copy"
    )
    # Hard-coded, so this stands alone: line 1 is the runbook's title and line
    # 10**6 is past its end. Neither can be inside §4.1.
    for outside in (1, 10**6):
        assert not _is_sanctioned("docs/DEPLOY_FLY.md", outside, "irrelevant"), (
            f"line {outside} of the runbook is not §4.1 and must not be exempt -- a "
            f"window that covers the whole document is not a window."
        )

    # The allowlist: LINE-scoped through the anchor, not file-scoped.
    _rel, anchor, _reason = _ALLOWED_COPIES[0]
    assert _is_sanctioned("scripts/check_bundle_key.sh", 150, f"echo '{anchor}' >&2"), (
        "the allowlisted incident-remediation hint must stay exempt"
    )
    for line_no in (1, 150, 99_999):
        assert not _is_sanctioned(
            "scripts/check_bundle_key.sh",
            line_no,
            "fly deploy --build-arg VERSION=$(git describe --tags --always)",
        ), (
            f"a DIFFERENT `fly deploy` command at line {line_no} of the allowlisted "
            f"file must not inherit its exemption -- that is exactly the hole a "
            f"file-scoped allowlist had."
        )
    # A command that shares the anchor's opening words but is not the anchor:
    # matching a PREFIX of the anchor rather than the whole of it re-opens the
    # file-scoped hole for anything starting `fly deploy --app cit...`.
    assert not _is_sanctioned(
        "scripts/check_bundle_key.sh",
        150,
        "fly deploy --app citevyn --build-arg VERSION=1.2.3",
    ), "a different command sharing the anchor's first words must not be exempt"
    # A SECOND command appended to the anchor's OWN line: the same hole one line
    # narrower, and `anchor in body` alone said yes to it.
    assert not _is_sanctioned(
        "scripts/check_bundle_key.sh",
        150,
        f"echo '{anchor}' ; fly deploy --app citevyn --build-arg VITE_API_DEMO_KEY=local-demo-key",
    ), "a second command appended to the anchor's line must not ride its exemption"

    for unexempt in (
        "docs/RELEASE_CANDIDATE_v0.12.0.md",
        "fly.toml",
        "docs/SOME_NEW_RUNBOOK.md",
        "scripts/some_new_script.sh",
    ):
        assert not _is_sanctioned(unexempt, 1, anchor), (
            f"{unexempt} is in neither exemption and must not be sanctioned -- an "
            f"exemption that answers yes to everything is not an exemption."
        )


def test_no_second_copy_of_the_deploy_command_exists() -> None:
    """#388. The absence gate.

    RED if the `fly deploy --build-arg VERSION=$(...)` line is put back into
    `fly.toml`'s `[build.args]` comment or into
    `docs/RELEASE_CANDIDATE_v0.12.0.md`'s ordered-commands block -- both proven
    by doing it.
    """
    offenders = _unsanctioned_deploy_copies()
    assert not offenders, (
        "a copy-pasteable `fly deploy` command lives outside docs/DEPLOY_FLY.md "
        "§4.1:\n  " + "\n  ".join(offenders) + "\n"
        "Every such copy has gone stale: the two removed in #388 had each missed "
        "BOTH #296 (VITE_API_DEMO_KEY -- the bundle ships the public default and "
        "every browser call 401s while /health stays green) and #385 "
        "(--local-only). Point at §4.1 instead of retyping the command, or add "
        "the file to _ALLOWED_COPIES with a reason."
    )


def _stale_allowlist_entries(entries: tuple[tuple[str, str, str], ...], root: Path) -> list[str]:
    """Allowlist entries that no longer exempt EXACTLY ONE line under ``root``.

    A pure function of (entries, root) so the rule can be driven with synthetic
    input. The inlined version could not be: `scripts/check_bundle_key.sh` holds
    exactly one deploy copy, so dropping the `if anchor in body` filter produced
    an identical list and the mutation survived. A rule whose only input has one
    element cannot distinguish "matches the anchor" from "matches anything".
    """
    stale: list[str] = []
    for rel, anchor, _reason in entries:
        path = root / rel
        if not path.exists():
            stale.append(f"{rel} (file is gone)")
            continue
        text = path.read_text(encoding="utf-8")
        matched = [n for n, body in _deploy_copies(path.suffix.lower(), text) if anchor in body]
        if not matched:
            stale.append(f"{rel} (no `fly deploy` line contains the anchor {anchor!r})")
        elif len(matched) > 1:
            stale.append(f"{rel} (the anchor matches {len(matched)} lines: {matched})")
    return stale


def test_every_allowlisted_deploy_copy_still_exists() -> None:
    """Staleness partner for `_ALLOWED_COPIES`.

    An exemption that no longer matches anything is waiting to shelter the next
    copy that lands in that file. Asserted on the ANCHOR, not on "the file still
    has some `fly deploy` line": the weaker form is what made the old file-scoped
    allowlist unfalsifiable.
    """
    stale = _stale_allowlist_entries(_ALLOWED_COPIES, _REPO_ROOT)
    assert not stale, (
        f"these _ALLOWED_COPIES entries no longer exempt exactly one line: {stale}. "
        f"Fix the anchor or delete the entry, or the allowlist grows until the "
        f"guard covers nothing."
    )
    assert all(reason.strip() for _rel, _anchor, reason in _ALLOWED_COPIES), (
        "every _ALLOWED_COPIES entry must carry a written reason"
    )


def test_the_staleness_rule_is_keyed_on_the_ANCHOR_not_on_any_deploy_line(
    tmp_path: Path,
) -> None:
    """Driven with synthetic entries, because the real allowlist has one entry.

    Three cases the real repo cannot exercise: a file with a SECOND, unrelated
    deploy command (still live -- the anchor matches exactly one line), a file
    whose anchored line is GONE while another deploy command remains (stale, and
    the case the weaker rule could not see), and an anchor matching TWO lines.

    RED if `_stale_allowlist_entries` drops its `if anchor in body` filter, or
    stops requiring exactly one match.
    """
    anchor = "fly deploy --app citevyn --build-arg VITE_API_DEMO_KEY=X ..."
    other = "fly deploy --app citevyn --build-arg VERSION=1.2.3"
    entry = (("hint.sh", anchor, "a reason"),)

    (tmp_path / "hint.sh").write_text(f"#!/usr/bin/env bash\necho '{anchor}'\n", encoding="utf-8")
    assert _stale_allowlist_entries(entry, tmp_path) == [], "the live entry must not be stale"

    (tmp_path / "hint.sh").write_text(
        f"#!/usr/bin/env bash\necho '{anchor}'\n{other}\n", encoding="utf-8"
    )
    assert _stale_allowlist_entries(entry, tmp_path) == [], (
        "an unrelated second deploy command must not make the entry stale"
    )

    (tmp_path / "hint.sh").write_text(f"#!/usr/bin/env bash\n{other}\n", encoding="utf-8")
    stale = _stale_allowlist_entries(entry, tmp_path)
    assert stale and "anchor" in stale[0], (
        f"the anchored line is GONE and another deploy command has taken its place; "
        f"the entry is stale and must be reported: {stale}"
    )

    (tmp_path / "hint.sh").write_text(
        f"#!/usr/bin/env bash\necho '{anchor}'\necho '{anchor}'\n", encoding="utf-8"
    )
    stale = _stale_allowlist_entries(entry, tmp_path)
    assert stale and "2 lines" in stale[0], (
        f"an anchor matching two lines exempts more than it names: {stale}"
    )

    (tmp_path / "hint.sh").unlink()
    assert _stale_allowlist_entries(entry, tmp_path) == ["hint.sh (file is gone)"]


def test_a_section_that_is_not_there_yields_an_EMPTY_range() -> None:
    """`_section_line_range` must fail CLOSED, and only a synthetic doc shows it.

    Measured on the real runbook: renaming `### 4.1 ` to `### 4.2 ` made the old
    implementation return `(0, 908)` -- every line sanctioned. Other tests
    happened to redden on that, but a fail-open exemption is the wrong direction
    to be wrong in, and the real file always HAS §4.1.

    RED if `_section_line_range` goes back to `(start or 0, len(lines))`.
    """
    doc = "# Title\n\n### 4.2 Deploy\n\nbody\n\n## Next\n\ntail\n"
    lo, hi = _section_line_range(doc, "4.1")
    assert lo > hi, f"a missing section must yield an EMPTY range, got ({lo}, {hi})"
    for line_no in (0, 1, 5, 9, 10**6):
        assert not (lo <= line_no <= hi), f"line {line_no} fell inside a missing section"
    # ...and the partner: the same function DOES find a section that is there.
    assert _section_line_range(doc, "4.2") == (3, 6), _section_line_range(doc, "4.2")


def test_an_unreadable_directory_is_not_silently_dropped_from_the_scan(
    tmp_path: Path,
) -> None:
    """`os.walk`'s default `onerror=None` DISCARDS the OSError, so an unreadable
    directory would not appear in the enumeration and the absence gate would
    report clean over a tree it could not read.

    RED if `_scanned_files` drops its `onerror=_raise`.
    """
    if os.geteuid() == 0:  # pragma: no cover - CI runs unprivileged
        pytest.skip("running as root: permissions do not block the walk")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "notes.md").write_text("x\n", encoding="utf-8")
    blocked.chmod(0o000)
    try:
        with pytest.raises(OSError):
            _scanned_files(tmp_path)
    finally:
        blocked.chmod(0o755)
    # ...and the partner: once readable, the same tree scans cleanly.
    assert [p.name for p in _scanned_files(tmp_path)] == ["notes.md"]


# (name, suffix, text, must be flagged). The absence test above only ever sees a
# repo with ZERO offenders, so a hole in the scanner is invisible until someone
# writes that shape into a file. Each shape is pinned here instead.
_COPY_SHAPES: tuple[tuple[str, str, str, bool], ...] = (
    (
        "md fenced bash block",
        ".md",
        "text\n\n```bash\nfly deploy --build-arg VERSION=1.2.3\n```\n",
        True,
    ),
    (
        "md fenced block, no language",
        ".md",
        "text\n\n```\nfly deploy --build-arg VERSION=1.2.3\n```\n",
        True,
    ),
    (
        "md fence with the command split over continuations",
        ".md",
        "```bash\nfly deploy --app citevyn \\\n  --build-arg VERSION=1.2.3\n```\n",
        True,
    ),
    (
        "md fenced block whose command is commented out",
        ".md",
        "```bash\n# fly deploy --build-arg VERSION=1.2.3\n```\n",
        True,
    ),
    # The blind spot that was documented as "a shape this repo does not use",
    # measured FALSE: docs/DEMO_CHECKLIST.md:51-54 is exactly this.
    (
        "md four-space indented code block",
        ".md",
        "text\n\n    fly deploy --app citevyn --build-arg VERSION=1.2.3\n\ntail\n",
        True,
    ),
    (
        "md fence indented inside a list item",
        ".md",
        "- step:\n\n      ```bash\n      fly deploy --app citevyn --build-arg V=1\n      ```\n",
        True,
    ),
    (
        "toml comment",
        ".toml",
        '[build.args]\n  #   fly deploy --build-arg V=1\n  V = "dev"\n',
        True,
    ),
    ("sh comment", ".sh", "#!/usr/bin/env bash\n#   fly deploy --build-arg V=1\n", True),
    (
        "sh runnable line",
        ".sh",
        "#!/usr/bin/env bash\nfly deploy --app citevyn --build-arg V=1\n",
        True,
    ),
    (
        "sh echo that PRINTS the command for the operator",
        ".sh",
        '#!/usr/bin/env bash\necho "  fly deploy --app citevyn --build-arg V=1 ..." >&2\n',
        True,
    ),
    # SPELLINGS OF THE SAME COMMAND, each demonstrated MISSED by the old
    # `(?<!`)\bfly deploy\b(?=\s+-)` matcher.
    (
        "flyctl, the CLI's real binary name",
        ".md",
        "```bash\nflyctl deploy --app citevyn --build-arg V=1\n```\n",
        True,
    ),
    (
        "two spaces between fly and deploy",
        ".md",
        "```bash\nfly  deploy --app citevyn --build-arg V=1\n```\n",
        True,
    ),
    (
        "a tab between fly and deploy",
        ".md",
        "```bash\nfly\tdeploy --app citevyn --build-arg V=1\n```\n",
        True,
    ),
    (
        "positional build-context path before the flags",
        ".md",
        "```bash\nfly deploy . --build-arg VERSION=1.2.3\n```\n",
        True,
    ),
    (
        "global flag BEFORE the subcommand",
        ".md",
        "```bash\nfly -a citevyn deploy --build-arg VERSION=1.2.3\n```\n",
        True,
    ),
    # TWO global flags, so the prefix group has to REPEAT. With `?` instead of
    # `*` only the single-flag case above is caught.
    (
        "two global flags before the subcommand",
        ".md",
        "```bash\nfly -t TOKEN -a citevyn deploy --build-arg VERSION=1.2.3\n```\n",
        True,
    ),
    # A SINGLE-dash flag after the subcommand. `_HAS_FLAG_RE` narrowed to `--`
    # reads this as a mention and lets a real command through.
    (
        "single-dash flag after the subcommand",
        ".md",
        "```bash\nfly deploy -a citevyn\n```\n",
        True,
    ),
    # The word "image" without the FLAG. Loosening the rollback exemption to a
    # substring exempts every deploy command whose comment mentions an image.
    (
        "a deploy command that merely mentions the word image",
        ".md",
        "```bash\nfly deploy --app citevyn --now  # rebuilds the image\n```\n",
        True,
    ),
    (
        "backticked command inside a toml comment",
        ".toml",
        "#   `fly deploy --app citevyn --build-arg VERSION=1.2.3`\n",
        True,
    ),
    (
        "backticked command inside a sh comment",
        ".sh",
        "#!/usr/bin/env bash\n#   `fly deploy --app citevyn --build-arg VERSION=1.2.3`\n",
        True,
    ),
    # ...and the shapes that must NOT be flagged.
    (
        "md prose mention",
        ".md",
        "Both are owner-gated (`fly deploy` is classifier-blocked) so nobody runs them.\n",
        False,
    ),
    (
        "md table cell",
        ".md",
        "| [#1](u) | the owner must still spend the `fly deploy` + re-embed cost | a |\n",
        False,
    ),
    (
        "md fenced --image rollback (passes no build args)",
        ".md",
        "```bash\nfly releases\nfly deploy --image <previous-image-ref>   # redeploy\n```\n",
        False,
    ),
    (
        "toml comment that merely NAMES the command in backticks",
        ".toml",
        "# Runbook: docs/DEPLOY_FLY.md (read it before your first `fly deploy`).\n",
        False,
    ),
    (
        "md fenced pointer that names no command",
        ".md",
        "```bash\n# Run docs/DEPLOY_FLY.md §4.1 verbatim. Do not retype it here.\nfly logs\n```\n",
        False,
    ),
    # The MENTION cases the dropped `(?=\s+-)` lookahead used to handle, now
    # handled by `_HAS_FLAG_RE` instead: no flag after the subcommand.
    (
        "sh comment that merely names the command",
        ".sh",
        "#!/usr/bin/env bash\n# Read docs/DEPLOY_FLY.md before your first `fly deploy`.\n",
        False,
    ),
    (
        "md fenced mention with no flag anywhere after it",
        ".md",
        "```bash\n# `fly deploy` is owner-gated; see §4.1.\nfly logs\n```\n",
        False,
    ),
    (
        "a hyphen that is not a flag",
        ".sh",
        "#!/usr/bin/env bash\n# `fly deploy` costs a re-embed and is owner-gated.\n",
        False,
    ),
    # The flag is BEFORE the subcommand and nowhere after it, so this is a
    # mention. Searching the whole line for a flag instead of the text after
    # `deploy` flags it -- measured surviving until this case existed.
    (
        "a flag mentioned before the subcommand only",
        ".sh",
        "#!/usr/bin/env bash\n# See --help, then run `fly deploy` from the runbook.\n",
        False,
    ),
)


@pytest.mark.parametrize(
    ("name", "suffix", "text", "flagged"), _COPY_SHAPES, ids=lambda v: str(v)[:40]
)
def test_the_deploy_copy_scanner_flags_exactly_the_stale_shapes(
    name: str, suffix: str, text: str, flagged: bool
) -> None:
    found = _unsanctioned_in(suffix, text)
    if flagged:
        assert found, f"{name}: a stale copy was NOT detected"
    else:
        assert not found, f"{name}: a legitimate line was flagged as a stale copy: {found}"


_CMD = "fly deploy --app citevyn --build-arg V=1"


@pytest.mark.parametrize(
    ("name", "template", "in_code"), _CODE_BLOCK_SHAPES, ids=lambda v: str(v)[:44]
)
def test_the_markdown_code_block_tracker_is_flavour_aware(
    name: str, template: str, in_code: bool
) -> None:
    """A `.md` command is inside a code block, or it is not. No third answer.

    Getting the boundary wrong is the difference between scanning prose (false
    positives) and scanning nothing (silent green).

    RED if `_FENCE_RE` drops the tilde flavour or its ` {0,3}` indent tolerance,
    stops requiring the closer to match the opener's character or length, accepts
    a closer carrying an info string, or if `_INDENTED_CODE_RE` is not consulted.
    """
    found = _deploy_copies(".md", template.format(p=_CMD))
    if in_code:
        assert found, f"{name}: a command inside a code block was not scanned"
    else:
        assert not found, f"{name}: a line OUTSIDE any code block was scanned: {found}"


def test_the_shared_code_block_tracker_has_not_drifted() -> None:
    """The twin block in `test_backlog_table_renders.py` must be BYTE-IDENTICAL.

    "Both files pin the same shape table so a divergence reddens whichever copy
    drifted" was FALSE as written: there was no cross-file comparison at all,
    only two independent tables sharing a name, and widening `_FENCE_RE` in this
    file alone left both files green (144 passed).

    RED if either copy of the block between the markers is edited alone.
    """
    pattern = re.compile(
        r"^# --- BEGIN SHARED CODE-BLOCK TRACKER.*?^# --- END SHARED CODE-BLOCK TRACKER[^\n]*\n",
        re.S | re.M,
    )
    here = pattern.search(Path(__file__).read_text(encoding="utf-8"))
    there = pattern.search(_TWIN.read_text(encoding="utf-8"))
    assert here is not None and there is not None, (
        f"the shared-tracker markers are missing from "
        f"{'this file' if here is None else _TWIN.name} -- the two copies can no longer "
        f"be compared, so the drift this test exists to catch is invisible again."
    )
    assert here.group(0) == there.group(0), (
        "the shared code-block tracker has DRIFTED between "
        f"{Path(__file__).name} and {_TWIN.name}. Apply the change to both copies "
        "byte for byte, or the two guards disagree about what a code block is."
    )
    # The partner: the block really has content, so byte-equality is not two
    # empty strings agreeing with each other.
    assert "_INDENTED_CODE_RE" in here.group(0) and len(here.group(0)) > 1000


def test_an_image_rollback_carrying_a_build_arg_is_not_exempt() -> None:
    """The `--image` exemption must not become a smuggling route.

    `fly deploy --image` is exempt because it passes no build args and so cannot
    drift the way #296 and #385 drifted. The exemption is conditional on that, in
    BOTH `--build-arg` spellings, since `_classify` accepts the equals form while
    its sibling test searches for the space form only.

    RED if the `and not _BUILD_ARG_ANY_RE.search(body)` clause is dropped from
    `_unsanctioned_in`.
    """
    bare = "```bash\nfly deploy --image sha256:abc\n```\n"
    assert not _unsanctioned_in(".md", bare), (
        "a plain --image rollback passes no build args and must stay exempt"
    )
    for spelling in ("--build-arg VERSION=1.2.3", "--build-arg=VERSION=1.2.3"):
        smuggled = f"```bash\nfly deploy --image sha256:abc {spelling}\n```\n"
        assert _unsanctioned_in(".md", smuggled), (
            f"an --image command carrying {spelling!r} rode the rollback exemption "
            f"straight past the guard"
        )
