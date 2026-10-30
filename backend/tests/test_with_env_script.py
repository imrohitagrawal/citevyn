"""``scripts/with_env.py`` loads a dotenv file exactly as python-dotenv reads it.

The Makefile runs the development commands through this wrapper so the backend
itself reads no file (see ``test_settings_reads_no_env_file.py``). It must read a
file the way ``Settings`` used to (python-dotenv, through pydantic-settings), not
the way ``uv run --env-file`` does: uv 0.11.18 cuts an unquoted ``ab$cdef`` to
``ab``, echoes a line it cannot parse to stderr, and drops every later line with
exit 0. python-dotenv itself silently skips an unparsable line, and an
unterminated quote swallows the rest of the file, so the wrapper also refuses a
malformed file outright (by line number, never echoing the line).

Every value below is a placeholder.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

_WRAPPER = Path(__file__).resolve().parents[2] / "scripts" / "with_env.py"

_PARITY_FILE = """\
# a comment line
export EXPORTED=exported-placeholder
DOUBLE="double quoted placeholder"
SINGLE='single quoted placeholder'
EMPTY=
UNQUOTED_DOLLAR=ab$cdef
DOUBLE_DOLLAR="ab$cdef"
SOURCE_NAME=src-placeholder
DOUBLE_BRACE="x${SOURCE_NAME}y"
SINGLE_BRACE='x${SOURCE_NAME}y'
INLINE=inline-placeholder # trailing comment
DUPLICATE=first-placeholder
DUPLICATE=second-placeholder
SHELL_WINS=from-the-file
"""

_NAMES = [
    "EXPORTED",
    "DOUBLE",
    "SINGLE",
    "EMPTY",
    "UNQUOTED_DOLLAR",
    "DOUBLE_DOLLAR",
    "SOURCE_NAME",
    "DOUBLE_BRACE",
    "SINGLE_BRACE",
    "INLINE",
    "DUPLICATE",
    "SHELL_WINS",
]

# Prints what the child process sees for each name, so the test reads the
# environment the wrapped command actually got.
_DUMP = "import json, os, sys; print(json.dumps({n: os.environ.get(n) for n in sys.argv[1:]}))"


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _NAMES}
    env.update(extra)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )


def _dump_through_wrapper(
    env_file: Path, env: dict[str, str], *flags: str
) -> subprocess.CompletedProcess[str]:
    return _run([*flags, str(env_file), "--", sys.executable, "-c", _DUMP, *_NAMES], env)


def test_values_match_python_dotenv_and_the_shell_wins(tmp_path: Path) -> None:
    # Red when: the wrapper parses differently from python-dotenv (uv's parser, a
    # hand-rolled one), or lets the file beat a variable already set in the shell.
    env_file = tmp_path / "dev.env"
    env_file.write_text(_PARITY_FILE, encoding="utf-8")

    result = _dump_through_wrapper(env_file, _clean_env(SHELL_WINS="from-the-shell"))
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)

    expected = dict(dotenv_values(env_file, encoding="utf-8"))
    expected["SHELL_WINS"] = "from-the-shell"
    assert seen == expected

    # The cases that differ between parsers, pinned by value so the comparison
    # above cannot pass by both sides drifting together.
    assert seen["UNQUOTED_DOLLAR"] == "ab$cdef"  # uv 0.11.18 gives "ab"
    assert seen["DOUBLE_DOLLAR"] == "ab$cdef"
    assert seen["DOUBLE_BRACE"] == "xsrc-placeholdery"
    # python-dotenv 1.2.2 expands ${NAME} inside single quotes too, unlike a shell.
    # Measured, and kept: it is what Settings read before, so values do not change.
    assert seen["SINGLE_BRACE"] == "xsrc-placeholdery"
    assert seen["EMPTY"] == ""
    assert seen["EXPORTED"] == "exported-placeholder"
    assert seen["INLINE"] == "inline-placeholder"
    assert seen["DUPLICATE"] == "second-placeholder"


def test_without_a_shell_value_the_file_value_is_used(tmp_path: Path) -> None:
    # Partner to the shell-wins case: the file must be able to win when the shell is silent.
    # Red when: the wrapper loads nothing at all (every name would be None).
    env_file = tmp_path / "dev.env"
    env_file.write_text(_PARITY_FILE, encoding="utf-8")
    result = _dump_through_wrapper(env_file, _clean_env())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["SHELL_WINS"] == "from-the-file"


@pytest.mark.parametrize(
    ("bad_line", "secret_part"),
    [
        # A bare name with no `=`: uv echoed it to stderr and dropped later lines.
        ("PLACEHOLDER_TOKEN_wpe7x2q9", "PLACEHOLDER_TOKEN_wpe7x2q9"),
        # An unterminated quote: python-dotenv drops this line and every one after it.
        ('PLACEHOLDER_KEY="wpe-unterminated-value', "wpe-unterminated-value"),
        # No name at all.
        ("=wpe-nameless-value", "wpe-nameless-value"),
        # Names no shell could set: python-dotenv accepts them, the wrapper must not.
        ("PLACE-HOLDER=wpe-dashed-value", "wpe-dashed-value"),
        ("1PLACEHOLDER=wpe-digit-value", "wpe-digit-value"),
        # The same name as line 1 in another case: Settings ignores case, so which
        # of the two it would read is not obvious. Refused.
        ("good_before=wpe-case-twin-value", "wpe-case-twin-value"),
    ],
)
def test_a_malformed_line_fails_by_line_number_and_prints_no_value(
    tmp_path: Path, bad_line: str, secret_part: str
) -> None:
    # Red when: a malformed line is skipped (exit 0, the command runs on half the file),
    # or the message stops naming the line, or it echoes any part of the line.
    env_file = tmp_path / "dev.env"
    env_file.write_text(f"GOOD_BEFORE=1\n{bad_line}\nGOOD_AFTER=2\n", encoding="utf-8")
    marker = "the-wrapped-command-ran"
    result = _run([str(env_file), "--", sys.executable, "-c", f"print({marker!r})"], _clean_env())
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "line 2" in output
    assert marker not in output
    assert secret_part not in output


def test_the_default_file_may_be_missing(tmp_path: Path) -> None:
    # Red when: a missing default file (CI, a fresh clone) stops the command, or the
    # notice goes away so nobody can tell no file was read.
    missing = tmp_path / "absent.env"
    result = _dump_through_wrapper(missing, _clean_env(), "--optional")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["SHELL_WINS"] is None
    assert str(missing) in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1


def test_an_explicit_file_that_is_missing_fails(tmp_path: Path) -> None:
    # Red when: an explicitly chosen file that does not exist is treated like the
    # optional default, so the command runs on settings the caller did not ask for.
    missing = tmp_path / "absent.env"
    marker = "the-wrapped-command-ran"
    result = _run([str(missing), "--", sys.executable, "-c", f"print({marker!r})"], _clean_env())
    assert result.returncode != 0
    assert marker not in result.stdout
    assert str(missing) in result.stderr


def test_the_wrapped_command_exit_code_is_kept(tmp_path: Path) -> None:
    # Red when: the wrapper swallows the command's failure (a `make migrate` that
    # failed would report success).
    env_file = tmp_path / "dev.env"
    env_file.write_text("A=1\n", encoding="utf-8")
    result = _run([str(env_file), "--", sys.executable, "-c", "raise SystemExit(7)"], _clean_env())
    assert result.returncode == 7


_SETTINGS_DB_URL = "from app.core.config import Settings; print(Settings().database_url)"


def _settings_env(**extra: str) -> dict[str, str]:
    env = _clean_env(PYTHONPATH=str(_WRAPPER.parents[1] / "backend"), **extra)
    for name in [k for k in env if k.upper() == "CITEVYN_DATABASE_URL"]:
        del env[name]
    env.update(extra)
    return env


def test_a_name_in_another_case_does_not_beat_the_shell(tmp_path: Path) -> None:
    # Red when: the shell-wins check compares names case-sensitively. Settings matches
    # names ignoring case, so a lowercase name from the file would beat the shell's.
    env_file = tmp_path / "dev.env"
    env_file.write_text("citevyn_database_url=sqlite:///from-the-file\n", encoding="utf-8")
    result = _run(
        [str(env_file), "--", sys.executable, "-c", _SETTINGS_DB_URL],
        _settings_env(CITEVYN_DATABASE_URL="sqlite:///from-the-shell"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sqlite:///from-the-shell"


def test_a_name_in_another_case_still_loads_when_the_shell_is_silent(tmp_path: Path) -> None:
    # Partner: the case-blind check must not drop the file's value outright.
    # Red when: a lowercase name in the file is skipped even with no shell value.
    env_file = tmp_path / "dev.env"
    env_file.write_text("citevyn_database_url=sqlite:///from-the-file\n", encoding="utf-8")
    result = _run([str(env_file), "--", sys.executable, "-c", _SETTINGS_DB_URL], _settings_env())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sqlite:///from-the-file"


def _unreadable(tmp_path: Path, kind: str) -> Path:
    path = tmp_path / f"{kind}.env"
    if kind == "dangling-symlink":
        path.symlink_to(tmp_path / "nowhere.env")
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("A=1\n", encoding="utf-8")
        path.chmod(0o000)
    return path


@pytest.mark.parametrize("kind", ["dangling-symlink", "directory", "no-permission"])
def test_optional_covers_only_a_path_that_does_not_exist(tmp_path: Path, kind: str) -> None:
    # Red when: `--optional` treats a path that EXISTS but cannot be read (a broken
    # link, a folder, no permission) as "not found" and runs without the file.
    if kind == "no-permission" and os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    path = _unreadable(tmp_path, kind)
    marker = "the-wrapped-command-ran"
    result = _run(
        ["--optional", str(path), "--", sys.executable, "-c", f"print({marker!r})"], _clean_env()
    )
    assert result.returncode == 2
    assert marker not in result.stdout
    assert str(path) in result.stderr
    assert "not found" not in result.stderr
    assert "Traceback" not in result.stderr


def test_a_byte_order_mark_does_not_glue_to_the_first_name(tmp_path: Path) -> None:
    # Red when: the file is read as plain utf-8, so a BOM becomes part of the first
    # name and that setting silently never arrives.
    env_file = tmp_path / "dev.env"
    env_file.write_bytes(b"\xef\xbb\xbfEXPORTED=bom-placeholder\n")
    result = _dump_through_wrapper(env_file, _clean_env())
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["EXPORTED"] == "bom-placeholder"


@pytest.mark.parametrize(
    "content",
    [b"A=wpe-bin-value\xff\xfe\n", b"A=wpe-bin-value\x00tail\n"],
    ids=["not-utf8", "nul-byte"],
)
def test_a_file_that_is_not_text_fails_cleanly(tmp_path: Path, content: bytes) -> None:
    # Red when: undecodable bytes or a NUL crash with a traceback (which can quote the
    # file), or are loaded anyway.
    env_file = tmp_path / "dev.env"
    env_file.write_bytes(content)
    result = _run([str(env_file), "--", sys.executable, "-c", "print('ran')"], _clean_env())
    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "UTF-8" in result.stderr
    assert "Traceback" not in output
    assert "wpe-bin-value" not in output
    assert "ran" not in result.stdout
