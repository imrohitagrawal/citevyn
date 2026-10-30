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
- A variable already set in the environment wins over the file.
- A line python-dotenv cannot parse, or a name with no ``=``, stops the run with
  exit 2 and names the line number only. python-dotenv would skip it (and an
  unterminated quote would swallow every line after it), so the command would run
  on part of the file. Nothing from the file is ever printed.
- A missing file stops the run, unless ``--optional`` is given (the Makefile's
  default file, absent in CI and on a fresh clone): then one line says so and the
  command runs on the process environment.

Needs python-dotenv, which the backend environment has (pydantic-settings
depends on it): run it with the backend's interpreter, as the Makefile does.
"""

from __future__ import annotations

import os
import sys

_USAGE = "usage: with_env.py [--optional] ENV_FILE -- COMMAND [ARG...]"


def _fail(message: str, code: int = 2) -> int:
    print(f"with_env: {message}", file=sys.stderr)
    return code


def main(argv: list[str]) -> int:
    optional = bool(argv) and argv[0] == "--optional"
    if optional:
        argv = argv[1:]
    if len(argv) < 3 or argv[1] != "--" or argv[0].startswith("-"):
        return _fail(_USAGE)
    path, command = argv[0], argv[2:]

    if not os.path.isfile(path):
        if not optional:
            return _fail(f"{path} not found (it was named explicitly, so it must exist)")
        print(
            f"with_env: {path} not found; running on the process environment only",
            file=sys.stderr,
        )
    else:
        from dotenv import dotenv_values
        from dotenv.parser import parse_stream

        with open(path, encoding="utf-8") as stream:
            bad = [
                binding.original.line
                for binding in parse_stream(stream)
                if binding.error or (binding.key is not None and binding.value is None)
            ]
        if bad:
            label = "line" if len(bad) == 1 else "lines"
            numbers = ", ".join(str(n) for n in bad)
            return _fail(f"{path}: {label} {numbers} not NAME=value; fix and run again")
        for name, value in dotenv_values(path, encoding="utf-8").items():
            if value is not None:
                os.environ.setdefault(name, value)

    try:
        os.execvp(command[0], command)
    except OSError as exc:
        return _fail(f"cannot run {command[0]}: {exc.strerror}", 127)
    return 0  # not reached: execvp replaces this process


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
