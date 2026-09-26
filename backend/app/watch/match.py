"""Does a change match a watch? (ADR-0005 §5, Phase 7C.) Pure, no I/O.

* ``page``: the change is to that page.
* ``term``: the text appears, ignoring case, in a line the change added or
  removed. Only changed lines count: a term that merely sits on a page that
  changed elsewhere is not a match.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def matches(kind: str, value: str, *, url: str, lines: Mapping[str, Any] | None) -> bool:
    if kind == "page":
        return url == value
    if kind == "term":
        needle = value.casefold()
        for key in ("added", "removed"):
            found: list[object] = list((lines or {}).get(key) or [])
            if any(isinstance(line, str) and needle in line.casefold() for line in found):
                return True
        return False
    return False


__all__ = ["matches"]
