"""Content contract for the shipped corpus under ``app/worker/sources``.

Live QA found "What is Codex?" answering "OpenAI's command-line coding agent" —
too narrow. The root cause is that these source docs are hand-authored
paraphrases, so a product definition is only as broad as whoever wrote it, and
nothing guarded the breadth.

Why this file exists rather than a golden/eval case: the eval harness builds its
corpus from :func:`tests.conftest.seed_catalog`, not from the real
``app/worker/sources/*.md`` files (see ``backend/tests/eval/retrieval.py``), so
no eval case can catch a regression in the SHIPPED corpus. These tests read the
real files through the real fetcher — the same bytes the worker ingests.

The assertions are deliberately about *breadth*, not exact wording, so the prose
stays editable.
"""

from __future__ import annotations

import re

import pytest

from app.worker.allowlist import get_source
from app.worker.fetchers import LocalFetcher

# Surfaces each product genuinely ships, beyond the terminal. A definition that
# mentions none of these has narrowed back to "it's a CLI".
#
# Matched on WORD BOUNDARIES, not as substrings: a bare ``"ide" in text`` also
# matches "inside", "provides", "wider" and "override", all of which already
# occur in these files — so the guard would pass on "Codex runs inside your
# terminal", exactly the regression it exists to catch.
_NON_TERMINAL_SURFACES = (
    "desktop",
    "IDE",
    "VS Code",
    "JetBrains",
    "web app",
    "cloud",
    "ChatGPT",
)


def _names_a_non_terminal_surface(text: str) -> bool:
    return any(
        re.search(rf"\b{re.escape(surface)}\b", text, re.IGNORECASE)
        for surface in _NON_TERMINAL_SURFACES
    )


def _overview(source_name: str) -> str:
    """Return the Overview section of a shipped source doc."""
    text = LocalFetcher().fetch(get_source(source_name))
    _, _, after = text.partition("## Overview")
    body, _, _ = after.partition("\n## ")
    assert body.strip(), (
        f"{source_name}.md has no '## Overview' section — the heading was "
        f"renamed or removed, so this guard is no longer reading the definition"
    )
    return body


@pytest.mark.parametrize("source_name", ["codex", "claude_code"])
def test_overview_is_not_terminal_only(source_name: str) -> None:
    """The product definition must name a surface beyond the terminal.

    Guards the live-QA regression: an Overview that describes only a
    command-line tool makes "What is Codex?" answer too narrowly.
    """
    overview = _overview(source_name)
    assert _names_a_non_terminal_surface(overview), (
        f"{source_name}.md Overview names no non-terminal surface; "
        f"expected one of {_NON_TERMINAL_SURFACES}"
    )


@pytest.mark.parametrize(
    ("source_name", "phrase"),
    [
        ("codex", "command-line coding agent"),
        ("claude_code", "agentic coding tool that runs in the terminal"),
    ],
)
def test_overview_drops_the_narrow_framing(source_name: str, phrase: str) -> None:
    """The exact narrow phrasings that caused the bug must not come back."""
    assert phrase.lower() not in _overview(source_name).lower()


def test_concepts_glossary_agrees_that_tools_are_multi_surface() -> None:
    """The glossary's tool list is retrieved for "which of these are LLMs?".

    It lives in a different file from the product docs, so it drifted narrow
    independently — and its summary sentence sits in the same chunk as the
    bullets, which is how a self-contradicting chunk reached retrieval.
    """
    text = LocalFetcher().fetch(get_source("concepts"))
    _, _, after = text.partition("## Which tools here are built on LLMs")
    section, _, _ = after.partition("\n## ")
    assert section.strip(), (
        "concepts.md has no 'Which tools here are built on LLMs' section — "
        "the heading changed, so this guard is reading nothing"
    )
    # Narrow framing, tolerant of rewording ("terminal-based coding agent" etc.).
    narrow = re.search(r"\bterminal[- ]\w*\s*coding (agent|tool)\b", section, re.IGNORECASE)
    assert narrow is None, (
        f"the glossary summary still calls these tools terminal-only "
        f"({narrow.group(0)!r} at offset {narrow.start()}), contradicting the "
        f"bullets directly above it — they sit in the SAME retrieved chunk"
    )
    for product in ("Claude Code", "Codex"):
        bullets = [
            line for line in section.splitlines() if line.strip().startswith(f"- {product} is")
        ]
        assert bullets, (
            f"concepts.md has no '- {product} is …' bullet; the glossary bullet "
            f"format changed, so this guard can no longer find the definition"
        )
        # The bullet wraps onto continuation lines, so scan to the next bullet.
        start = section.index(bullets[0])
        rest = section[start + len(bullets[0]) :]
        entry = bullets[0] + rest.partition("\n- ")[0]
        assert _names_a_non_terminal_surface(entry), (
            f"glossary bullet for {product} names no non-terminal surface: {entry!r}"
        )


def test_codex_cli_only_sections_are_scoped_in_their_own_heading() -> None:
    """Every CLI-only section must carry "CLI" in its own heading.

    This is a retrieval property, not a prose preference. ``chunker.py`` emits
    one chunk per ``##`` section as ``"{title} — {heading}. {body}"``, so the
    heading is the ONLY scoping a retrieved chunk carries with it. A scoping
    sentence in the Overview does not travel: "how do I install Codex?" returns
    the installation chunk alone.

    Broadening the doc title from "Codex CLI Reference" to "Codex Reference"
    removed the word CLI from every chunk's prefix, which is what made this
    necessary — otherwise ``Codex Reference — Authentication. Codex reads its
    credentials from OPENAI_API_KEY…`` reads as a claim about ChatGPT Codex too.
    """
    text = LocalFetcher().fetch(get_source("codex"))
    headings = re.findall(r"^## (.+)$", text, re.MULTILINE)
    assert headings, "codex.md has no '## ' sections"
    unscoped = [
        h for h in headings if h != "Overview" and not re.search(r"\bCLI\b", h, re.IGNORECASE)
    ]
    assert not unscoped, (
        f"codex.md sections {unscoped} describe CLI-only behaviour but carry no "
        f"'CLI' marker in the heading. Since the doc title no longer says CLI, "
        f"these chunks reach the generator with nothing scoping them to the CLI."
    )
