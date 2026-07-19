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

import pytest

from app.worker.allowlist import get_source
from app.worker.fetchers import LocalFetcher

# Surfaces each product genuinely ships, beyond the terminal. A definition that
# mentions none of these has narrowed back to "it's a CLI".
_NON_TERMINAL_SURFACES = ("desktop", "IDE", "web app", "cloud", "ChatGPT")


def _overview(source_name: str) -> str:
    """Return the Overview section of a shipped source doc."""
    text = LocalFetcher().fetch(get_source(source_name))
    _, _, after = text.partition("## Overview")
    body, _, _ = after.partition("\n## ")
    assert body.strip(), f"{source_name}.md has no Overview section"
    return body


@pytest.mark.parametrize("source_name", ["codex", "claude_code"])
def test_overview_is_not_terminal_only(source_name: str) -> None:
    """The product definition must name a surface beyond the terminal.

    Guards the live-QA regression: an Overview that describes only a
    command-line tool makes "What is Codex?" answer too narrowly.
    """
    overview = _overview(source_name)
    assert any(surface.lower() in overview.lower() for surface in _NON_TERMINAL_SURFACES), (
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
    assert section.strip()
    assert "terminal coding agent" not in section, (
        "the glossary summary still calls these tools terminal-only, "
        "contradicting the bullets directly above it"
    )
    for product in ("Claude Code", "Codex"):
        bullet = next(
            line for line in section.splitlines() if line.strip().startswith(f"- {product} is")
        )
        # The bullet wraps, so scan from the bullet to the next bullet.
        start = section.index(bullet)
        rest = section[start + len(bullet) :]
        entry = bullet + rest.partition("\n- ")[0]
        assert any(surface.lower() in entry.lower() for surface in _NON_TERMINAL_SURFACES), (
            f"glossary bullet for {product} names no non-terminal surface"
        )


def test_codex_scopes_its_cli_only_details() -> None:
    """The Overview must not promise the CLI details generalize.

    ``codex.md`` documents npm/Homebrew install, flags, the sandbox, and a config
    file — all CLI-only. An Overview claiming the other surfaces "share the same
    core behavior" licenses the generator to answer "how do I install Codex in
    ChatGPT?" with ``npm install``.
    """
    overview = _overview("codex")
    assert "share the same core behavior" not in overview.lower()
    assert "cli" in overview.lower()
