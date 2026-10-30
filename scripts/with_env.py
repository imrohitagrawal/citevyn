#!/usr/bin/env python3
"""Run a command with a dotenv file loaded under the process environment.

Usage: with_env.py [--optional] ENV_FILE -- COMMAND [ARG...]

The backend's ``Settings`` reads no file; the Makefile runs the development
commands (``make api``, ``migrate``, ``seed``, ``eval`` and the others listed in
docs/ENVIRONMENT_FILES.md) through this script so they get ``backend/.env``.

- Parsing is python-dotenv's, the parser ``Settings`` used when it read the file
  itself, so a value means the same as before. Not ``uv run --env-file``: uv
  0.11.18 cuts an unquoted ``ab$cdef`` to ``ab`` and drops every line after one
  it cannot parse.
- A variable already set in the environment wins over the file, compared
  ignoring case, because ``Settings`` matches names ignoring case: a lowercase
  name in the file must not beat the shell's uppercase one.
- These stop the run with exit 2, naming the line number only: a line
  python-dotenv cannot parse, a name with no ``=``, a name a shell could not set
  (not ``[A-Za-z_][A-Za-z0-9_]*``), and a name that repeats an earlier one in a
  different case. python-dotenv would skip the first (and an unterminated quote
  would swallow every line after it), so the command would run on part of the
  file. A file that is not UTF-8 text (a NUL byte, undecodable bytes) also stops
  it. A leading byte-order mark is ignored. Nothing from the file is printed.
- A path that does not exist stops the run, unless ``--optional`` is given (the
  Makefile's default file, absent in CI and on a fresh clone): then one line says
  so and the command runs on the process environment. A path that exists but
  cannot be read as a file (a broken link, a folder, no permission) always stops it.

Needs python-dotenv (a declared backend dependency): run it with the backend's
interpreter, as the Makefile does.
"""

from __future__ import annotations

import io
import os
import re
import sys

_USAGE = "usage: with_env.py [--optional] ENV_FILE -- COMMAND [ARG...]"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _fail(message: str, code: int = 2) -> int:
    print(f"with_env: {message}", file=sys.stderr)
    return code


def _bad_lines(text: str) -> list[int]:
    """Line numbers python-dotenv cannot read, or whose name the shell could not hold."""
    from dotenv.parser import parse_stream

    bad: list[int] = []
    first_spelling: dict[str, str] = {}
    for binding in parse_stream(io.StringIO(text)):
        key = binding.key
        if binding.error:
            bad.append(binding.original.line)
        elif key is not None:
            twin = first_spelling.setdefault(key.lower(), key)
            if binding.value is None or not _NAME.match(key) or twin != key:
                bad.append(binding.original.line)
    return bad


def main(argv: list[str]) -> int:
    optional = bool(argv) and argv[0] == "--optional"
    if optional:
        argv = argv[1:]
    if len(argv) < 3 or argv[1] != "--" or argv[0].startswith("-"):
        return _fail(_USAGE)
    path, command = argv[0], argv[2:]

    if not os.path.lexists(path):
        if not optional:
            return _fail(f"{path} not found (it was named explicitly, so it must exist)")
        print(
            f"with_env: {path} not found; running on the process environment only",
            file=sys.stderr,
        )
    else:
        if not os.path.isfile(path):
            return _fail(f"{path} exists but is not a regular file (a broken link or a folder?)")
        try:
            with open(path, "rb") as stream:
                raw = stream.read()
        except OSError as exc:
            return _fail(f"cannot read {path}: {exc.strerror}")
        try:
            if b"\x00" in raw:
                raise ValueError
            text = raw.decode("utf-8-sig")
        except ValueError:  # UnicodeDecodeError is a ValueError
            return _fail(f"{path} is not valid UTF-8 text")

        bad = _bad_lines(text)
        if bad:
            label = "line" if len(bad) == 1 else "lines"
            numbers = ", ".join(str(n) for n in bad)
            return _fail(f"{path}: {label} {numbers} not NAME=value; fix and run again")

        from dotenv import dotenv_values

        already_set = {name.lower() for name in os.environ}
        for name, value in dotenv_values(stream=io.StringIO(text)).items():
            if value is not None and name.lower() not in already_set:
                os.environ[name] = value

    try:
        os.execvp(command[0], command)
    except OSError as exc:
        return _fail(f"cannot run {command[0]}: {exc.strerror}", 127)
    return 0  # not reached: execvp replaces this process


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
