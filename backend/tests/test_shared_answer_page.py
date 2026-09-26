"""The shared-answer page renderer (ADR-0005 §4, Phase 6C).

A shared page is PUBLIC and shows text a user typed (the question) and text the
model wrote (the answer), on our own domain. So this is a live XSS and phishing
boundary, unlike ``/about``: everything is escaped, nothing in the question or
the answer becomes a link, and a citation links only to an http(s) URL.

Each test says which change turns it red.
"""

from __future__ import annotations

from app.services.shared_answer_page import (
    format_day,
    render_answer_html,
    render_shared_answer_page,
)

CITES = [
    {
        "marker": 1,
        "title": "Permissions",
        "url": "https://docs.example.com/p",
        "docs_as_of": "2026-09-01",
    },
    {
        "marker": 2,
        "title": "Settings",
        "url": "https://docs.example.com/s",
        "docs_as_of": "2026-08-15",
    },
]


def _page(
    question: str = "How?",
    answer: str = "Like this [1].",
    citations: list[dict[str, object]] | None = None,
) -> str:
    return render_shared_answer_page(
        question=question,
        answer=answer,
        citations=CITES if citations is None else citations,
        docs_as_of="2026-08-15",
        shared_on="2026-09-26",
    )


def test_the_subset_renders_like_the_chat() -> None:
    """Bold, inline code, bullets and markers: the chat's subset (#303).
    Turns red if: any of the four stops rendering."""
    out = render_answer_html("Use **bold** and `code` [2].\n\n- one\n* two", markers={2})
    assert "<strong>bold</strong>" in out
    assert "<code>code</code>" in out
    assert '<a class="share-marker" href="#source-2">[2]</a>' in out
    assert "<ul><li>one</li><li>two</li></ul>" in out


def test_markup_in_the_answer_is_text() -> None:
    """Turns red if: the answer is not escaped (stored XSS on our domain)."""
    out = render_answer_html(
        '<script>alert(1)</script> <img src=x onerror="a()"> `<b>`', markers=set()
    )
    assert "<script>" not in out and "<img" not in out and "<b>" not in out
    assert "&lt;script&gt;" in out
    assert "<code>&lt;b&gt;</code>" in out


def test_markup_inside_bold_is_text() -> None:
    """Turns red if: text inside ** ** skips the escape."""
    out = render_answer_html("**<i>x</i>**", markers=set())
    assert "<strong>&lt;i&gt;x&lt;/i&gt;</strong>" in out


def test_a_marker_with_no_source_stays_text() -> None:
    """A [7] with no citation 7 is not a link to nowhere.
    Turns red if: every [n] becomes a link."""
    out = render_answer_html("See [7] and [1].", markers={1})
    assert "#source-7" not in out
    assert "[7]" in out
    assert "#source-1" in out


def test_unclosed_delimiters_are_literal() -> None:
    """Turns red if: an unclosed ** or ` swallows the rest of the answer."""
    out = render_answer_html("a **b and `c", markers=set())
    assert out == "<p>a **b and `c</p>"


def test_urls_in_the_answer_and_question_are_never_links() -> None:
    """Phishing: a user controls the question and can steer the answer.
    Turns red if: either is linkified."""
    page = _page(
        question="Go to https://evil.example/login", answer="Visit https://evil.example [1]"
    )
    assert 'href="https://evil.example' not in page
    assert "https://evil.example/login" in page


def test_citations_link_only_to_http_urls() -> None:
    """Turns red if: a javascript: (or other non-http) citation URL becomes a link."""
    page = _page(
        citations=[
            {"marker": 1, "title": "Bad", "url": "javascript:alert(1)", "docs_as_of": None},
            {"marker": 2, "title": "Good", "url": "https://docs.example.com/g", "docs_as_of": None},
        ],
        answer="x [1] [2]",
    )
    assert 'href="javascript' not in page
    assert '<a href="https://docs.example.com/g"' in page
    assert "Bad" in page


def test_a_citation_url_cannot_break_out_of_its_attribute() -> None:
    """Turns red if: the href is not attribute-escaped."""
    page = _page(
        citations=[
            {
                "marker": 1,
                "title": "T",
                "url": 'https://d.example/"><script>x()</script>',
                "docs_as_of": None,
            }
        ]
    )
    assert "<script>x()" not in page
    assert "&quot;&gt;&lt;script&gt;" in page


def test_the_page_shows_the_question_answer_sources_and_dates() -> None:
    """Turns red if: the frozen copy drops a part ADR-0005 names (answer,
    citations, crawl date) or the question."""
    page = _page(question="How do I <configure> it?")
    assert "How do I &lt;configure&gt; it?" in page
    assert "Like this" in page
    assert 'id="source-1"' in page and "Permissions" in page and "Settings" in page
    assert "Docs as of 15 Aug 2026." in page
    assert "26 Sep 2026" in page


def test_the_title_and_description_never_carry_user_text() -> None:
    """The <title> and meta description show in link previews: fixed text only.
    Turns red if: the question reaches either."""
    page = _page(question="FREE REFUND click here")
    head = page.split("</head>")[0]
    assert "FREE REFUND" not in head


def test_text_around_a_marker_inside_bold_is_escaped() -> None:
    """The inner loop escapes text between markers separately.
    Turns red if: either inner escape is dropped."""
    out = render_answer_html("**<b>see [1]</b> <i>**", markers={1})
    assert "<b>" not in out and "<i>" not in out
    assert (
        '<strong>&lt;b&gt;see <a class="share-marker" href="#source-1">[1]</a>'
        "&lt;/b&gt; &lt;i&gt;</strong>"
    ) in out


def test_an_odd_marker_value_never_breaks_the_page() -> None:
    """ "²".isdigit() is True but int("²") raises: a 500 on a public page.
    Turns red if: marker strings are checked with isdigit()."""
    page = _page(citations=[{"marker": "²", "title": "T", "url": None, "docs_as_of": None}])
    assert "T" in page
    assert 'id="source-' not in page


def test_no_empty_paragraph_without_a_date() -> None:
    """Turns red if: the page prints an empty <p> when no source has a date."""
    page = render_shared_answer_page(
        question="q", answer="a", citations=CITES, docs_as_of=None, shared_on="2026-09-26"
    )
    assert "<p></p>" not in page


def test_a_date_that_is_not_a_date_is_shown_as_it_is() -> None:
    """Turns red if: an impossible month or a non-date crashes or is reformatted."""
    assert format_day("2026-09-01") == "1 Sep 2026"
    assert format_day("2026-13-01") == "2026-13-01"
    assert format_day("soon") == "soon"


def test_a_paragraph_and_a_list_next_to_each_other_stay_apart() -> None:
    """No blank line between them, in either order.
    Turns red if: the switch does not close the open paragraph or list."""
    assert render_answer_html("Intro\n- one\nOutro", markers=set()) == (
        "<p>Intro</p><ul><li>one</li></ul><p>Outro</p>"
    )


def test_citation_urls_that_are_not_plain_links_are_text() -> None:
    """A URL that cannot be parsed, and one with a user name (it reads as the
    wrong host). Turns red if: either becomes a link, or the page crashes."""
    page = _page(
        citations=[
            {"marker": 1, "title": "Broken", "url": "http://[::1", "docs_as_of": None},
            {
                "marker": 2,
                "title": "Creds",
                "url": "https://docs.example.com@evil.example/",
                "docs_as_of": None,
            },
        ],
        answer="x [1] [2]",
    )
    assert "Broken" in page and "Creds" in page
    assert 'href="http://[' not in page
    assert 'href="https://docs.example.com@' not in page


def test_marker_values_other_than_plain_numbers() -> None:
    """True is an int in Python but not a citation number; "2" is.
    Turns red if: True becomes source 1, or a digit string is not read."""
    page = _page(
        citations=[
            {"marker": True, "title": "Flag", "url": None, "docs_as_of": None},
            {"marker": "2", "title": "Two", "url": None, "docs_as_of": None},
        ],
        answer="x [1] [2]",
    )
    assert 'id="source-1"' not in page
    assert 'id="source-2"' in page
    assert '<a class="share-marker" href="#source-2">[2]</a>' in page
    assert "#source-1" not in page
