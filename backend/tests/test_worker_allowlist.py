"""Tests for :mod:`app.worker.allowlist`."""

from __future__ import annotations

import ast
import io
import pathlib
import re
import tokenize

import pytest

from app.worker.allowlist import (
    MVP_SOURCES,
    get_source,
    list_source_names,
)

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
ALLOWLIST_PATH = BACKEND_ROOT / "app" / "worker" / "allowlist.py"
# Files whose prose describes the shipped corpus. Deliberately a short explicit
# list: a repo-wide sweep would hit CHANGELOG and BACKLOG entries, which are
# HISTORY and must keep saying what was true when they were written.
#
# THIS file is deliberately absent, and must stay absent: it has to contain the
# wrong strings in order to test for them, and every one of them is planted.
# The one live claim it carries — `test_mvp_sources_has_six_entries`' docstring —
# sits directly above `assert len(MVP_SOURCES) == 6`, so it cannot drift silently:
# the assertion goes red before the prose is wrong.
_FILES_DESCRIBING_THE_CORPUS = (
    ALLOWLIST_PATH,
    BACKEND_ROOT / "tests" / "test_messages_routes.py",
    # Found only when a reviewer went looking outside the set (#420): an operator
    # runbook stating the corpus size, correct today and previously unguarded.
    REPO_ROOT / "docs" / "DEPLOY_FLY.md",
)


def test_mvp_sources_is_a_tuple() -> None:
    """The allowlist is a frozen tuple, not a list — the list is compile-time fixed."""
    assert isinstance(MVP_SOURCES, tuple)


def test_mvp_sources_has_six_entries() -> None:
    """Six sources: the four product docs + About CiteVyn (#49) + the AI concepts/glossary
    (#112 follow-up, so conceptual questions like "what is an LLM?" answer instead of refuse)."""
    assert len(MVP_SOURCES) == 6


def test_mvp_source_names_match_demo_catalog() -> None:
    """Source names match the demo catalog in :func:`tests.conftest.seed_catalog`."""
    assert {s.name for s in MVP_SOURCES} == {
        "claude_api",
        "claude_code",
        "codex",
        "gemini_api",
        "citevyn",
        "concepts",
    }


def test_citevyn_source_uses_host_agnostic_relative_url() -> None:
    """The About-CiteVyn citation must not reference a domain we don't own —
    it uses a relative /about path (see allowlist rationale)."""
    spec = get_source("citevyn")
    assert spec.product_area == "citevyn"
    assert spec.source_url == "/about"


def test_mvp_source_names_are_unique() -> None:
    """Two sources with the same name would collide in :class:`IngestionJob`."""
    names = [s.name for s in MVP_SOURCES]
    assert len(set(names)) == len(names)


def test_get_source_returns_named_source() -> None:
    spec = get_source("claude_api")
    assert spec.name == "claude_api"
    assert spec.product_area == "claude_api"


def test_get_source_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError) as exc_info:
        get_source("does-not-exist")
    assert "does-not-exist" in str(exc_info.value)


def test_list_source_names_returns_tuple() -> None:
    names = list_source_names()
    assert isinstance(names, tuple)
    assert "claude_api" in names


def test_source_spec_is_frozen() -> None:
    """A spec is immutable; mutating a field raises ``FrozenInstanceError``."""
    from dataclasses import FrozenInstanceError

    spec = get_source("codex")
    with pytest.raises(FrozenInstanceError):
        spec.name = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The prose describing the allowlist must not drift from the allowlist (#420)
# ---------------------------------------------------------------------------
#
# ``test_mvp_sources_has_six_entries`` above already pins the CODE. What nothing
# pinned was the English: three separate comments stated a source count that was
# short by one, and two of them also enumerated the corpus while omitting the
# concepts/glossary source entirely — stale from the day it landed, and invisible
# because a comment cannot fail a build. The tests below close that gap from both
# sides: the enumeration must NAME every shipped source, and any count written in
# prose must MATCH the shipped count.
#
# This file is deliberately NOT in ``_FILES_DESCRIBING_THE_CORPUS``. It has to be
# able to write the wrong strings in order to test for them, which is exactly what
# the scanned files must never do.


def _prose_of(path: pathlib.Path) -> str:
    """Comments and docstrings only — never executable text.

    Reading a ``.py`` file whole would make the enumeration check pass on the
    ``SourceSpec(...)`` literals themselves, which is precisely the drift it
    exists to catch. ``tokenize`` gives the comment tokens exactly; docstrings
    come from the parsed AST.

    EVERY docstring, not just the module's. The first version called
    ``ast.get_docstring(ast.parse(source))``, which returns the module docstring
    alone — so a count written in a function or class docstring was invisible,
    and a reviewer demonstrated exactly that against this guard (#420). Markdown
    has no executable text to exclude, so the whole file is its prose.
    """
    source = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return source
    comments = [
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(source).readline)
        if tok.type == tokenize.COMMENT
    ]
    tree = ast.parse(source)
    docstrings = [ast.get_docstring(tree) or ""]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            docstrings.append(ast.get_docstring(node) or "")
    return "\n".join([*docstrings, *comments])


# A count written in English or digits, directly qualifying the corpus. Covers
# "five sources", "5 MVP sources", "five documents", "six docs", "five entries".
#
# A trailing "per"/"each"/"apiece" is excluded, because "one document per entry in
# MVP_SOURCES" is a RATE, not a count of the corpus — and reading it as a count
# made this guard red on correct prose the first time it ran.
#
# WHAT IT STILL CANNOT SEE, measured by an adversarial reviewer against this very
# guard (#420) rather than imagined, so the list is evidence and not a guess:
#
#   "The corpus ships five demo sources"   any adjective between number and noun
#   "The corpus covers five product areas" a paraphrase of the noun
#   "The MVP sources are five"             the number trailing the noun
#   "The four product docs plus ..."       a SUB-count, not a corpus count
#
# ("Half a dozen sources" and "twenty sources" were on that list too; both are
# closed below, since extending the vocabulary costs nothing and risks no false
# positive. The four above are the ones that cannot be closed cheaply.)
#
# The adjective case is deliberately NOT closed, and this is the interesting one:
# allowing one intervening word would flag "the four product docs", which is a
# correct sub-count of a category and would make the guard red on true prose.
# There is no regex that separates "five demo sources" from "four product docs"
# without understanding the sentence, so widening here buys bypass coverage at the
# price of false positives on correct text. Instead, the ENUMERATION test is the
# load-bearing guard — it compares against the live objects and has no English in
# it — and this one is the cheap second net for the exact shape that actually
# rotted. Say so plainly rather than implying the net is complete.
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "a dozen": 12,
    "half a dozen": 6,
}
# Longest alternative first, so "half a dozen" wins over "a dozen" and "nineteen"
# is not consumed as "nine". Regex alternation is first-match, not longest-match;
# relying on dict order here would make the result depend on insertion order.
_COUNT_ALTERNATIVES = "|".join(sorted(_COUNT_WORDS, key=len, reverse=True))
_CORPUS_NOUN = r"(?:MVP\s+)?(?:sources?|documents?|docs?|entries|entry)"
_NOT_A_RATE = r"(?!\s+(?:per|each|apiece)\b)"
_COUNT_RE = re.compile(
    rf"\b(\d+|{_COUNT_ALTERNATIVES})\s+{_CORPUS_NOUN}\b{_NOT_A_RATE}",
    re.IGNORECASE,
)
# A RANGE never states the size of a fixed tuple. "We ship 5-6 sources" was read
# as "6 sources" by the first version and passed, which is worse than missing it:
# the guard actively endorsed a claim that contradicts the list. Matched first and
# always rejected.
_RANGE_RE = re.compile(
    rf"\b(?:\d+|{_COUNT_ALTERNATIVES})\s*[-–—]\s*(?:\d+|{_COUNT_ALTERNATIVES})\s+{_CORPUS_NOUN}\b",
    re.IGNORECASE,
)


def _corpus_counts_claimed_in(prose: str) -> list[str]:
    """Every count-of-the-corpus claim in ``prose``, as written."""
    return [m.group(0) for m in _COUNT_RE.finditer(prose)]


def _corpus_ranges_claimed_in(prose: str) -> list[str]:
    """Every RANGE claimed of the corpus — always wrong, whatever the numbers."""
    return [m.group(0) for m in _RANGE_RE.finditer(prose)]


def test_the_allowlist_comment_names_every_shipped_source() -> None:
    """The MVP-source-list comment must enumerate every ``SourceSpec.title``.

    Turns red by: adding a seventh ``SourceSpec`` to ``MVP_SOURCES`` without
    adding its title to the comment block above the list (verified by doing
    exactly that — see the PR for #420).
    """
    prose = _prose_of(ALLOWLIST_PATH)
    assert prose.strip(), "extracted no prose from allowlist.py — the extractor is broken"
    missing = [s.title for s in MVP_SOURCES if s.title not in prose]
    assert not missing, (
        f"allowlist.py ships {[s.title for s in MVP_SOURCES]} but its comments never "
        f"mention {missing}; the enumeration has drifted from MVP_SOURCES"
    )
    # Non-vacuity partner: the check must be capable of reporting a miss. A title
    # that is NOT shipped must not be found, or "everything matches" would be an
    # artefact of scanning text that happens to contain anything.
    assert "Encyclopaedia Galactica Reference" not in prose


def test_no_prose_count_of_the_corpus_contradicts_mvp_sources() -> None:
    """Any count of the corpus written in prose must equal ``len(MVP_SOURCES)``.

    Turns red by: writing "the five MVP sources" in either scanned file (verified
    by planting exactly that string, see the PR for #420). The extractor's own
    ability to see such a claim is proved by the synthetic test below, so a green
    result here means "no contradicting claim", not "found nothing".
    """
    shipped = len(MVP_SOURCES)
    for path in _FILES_DESCRIBING_THE_CORPUS:
        assert path.exists(), f"{path} moved; this guard now covers nothing"
        prose = _prose_of(path)
        # Non-vacuity: if the extractor ever returned nothing, the loop below
        # would not execute and this test would pass having checked no text at
        # all. The enumeration test carries the same assertion; this one lacked
        # it until a reviewer pointed out the asymmetry (#420).
        assert prose.strip(), f"extracted no prose from {path.name} — the extractor is broken"
        ranges = _corpus_ranges_claimed_in(prose)
        assert not ranges, (
            f"{path.name} states the corpus size as a RANGE {ranges}; MVP_SOURCES is a "
            f"fixed tuple of {shipped}, so no range can be right"
        )
        for claim in _corpus_counts_claimed_in(prose):
            # The noun is the last token and an optional "MVP" sits before it;
            # what remains is the number, which may be several words ("half a
            # dozen"). Splitting on the FIRST space instead would read
            # "half a dozen sources" as the word "half".
            written = claim.rsplit(maxsplit=1)[0].lower().removesuffix(" mvp").strip()
            value = _COUNT_WORDS.get(written)
            if value is None:
                value = int(written) if written.isdigit() else None
            assert value == shipped, (
                f'{path.name} prose says "{claim}" but MVP_SOURCES ships {shipped}: '
                f"{[s.name for s in MVP_SOURCES]}"
            )


def test_the_count_detector_actually_sees_a_stale_claim() -> None:
    """Partner for the test above: prove the extractor is not blind.

    A guard asserting something is ABSENT is worthless if it cannot see the thing
    present. This drives the same regex over the exact sentences that were wrong,
    plus the digit and plural spellings a future edit might use.
    """
    assert _corpus_counts_claimed_in("The five MVP sources match the demo catalog") == [
        "five MVP sources"
    ]
    assert _corpus_counts_claimed_in("an active index, five documents — the four") == [
        "five documents"
    ]
    assert _corpus_counts_claimed_in("we ship 5 sources today") == ["5 sources"]
    assert _corpus_counts_claimed_in("six docs are indexed") == ["six docs"]
    # Spellings added after a reviewer walked past the first version (#420):
    # beyond twelve, and the two dozen-idioms.
    assert _corpus_counts_claimed_in("twenty sources are indexed") == ["twenty sources"]
    assert _corpus_counts_claimed_in("half a dozen sources ship today") == ["half a dozen sources"]
    assert _corpus_counts_claimed_in("a dozen docs ship today") == ["a dozen docs"]
    assert _corpus_counts_claimed_in("there are five entries in MVP_SOURCES") == ["five entries"]
    # And that it does NOT fire where there is no corpus count to be wrong about,
    # so the guard above cannot be reddened by correct prose. A trailing
    # "per"/"each" makes it a rate; the second line is the exact sentence that
    # tripped it during development.
    assert _corpus_counts_claimed_in("the allowlist is a frozen tuple") == []
    assert _corpus_counts_claimed_in("one document per entry in MVP_SOURCES") == []
    assert _corpus_counts_claimed_in("two sources each carry a chunk") == []
    # The documented blind spot, pinned as a blind spot so that closing it is a
    # deliberate act with a test change, not an accident. See the note above the
    # regex for why widening here would redden correct prose.
    assert _corpus_counts_claimed_in("the corpus ships five demo sources") == []
    assert _corpus_counts_claimed_in("the four product docs plus About CiteVyn") == []


def test_a_range_is_rejected_however_it_is_written() -> None:
    """A range never states the size of a fixed tuple.

    Turns red by: deleting the range assertion from the count test. The first
    version of that test read "we ship 5-6 sources" as "6 sources" and PASSED —
    endorsing a claim that contradicts ``MVP_SOURCES`` rather than merely
    missing it, which is the worse of the two failure modes.
    """
    assert _corpus_ranges_claimed_in("we ship 5-6 sources") == ["5-6 sources"]
    assert _corpus_ranges_claimed_in("five to six sources") == []  # prose, not a range
    assert _corpus_ranges_claimed_in("five-six documents") == ["five-six documents"]
    assert _corpus_ranges_claimed_in("6 sources ship today") == []
    # An en-dash and an em-dash are the same claim typed differently.
    assert _corpus_ranges_claimed_in("we ship 5–6 sources") == ["5–6 sources"]


def test_the_prose_extractor_sees_every_docstring_and_comment(
    tmp_path: pathlib.Path,
) -> None:
    """Partner for ``_prose_of`` itself, which previously had none (#420).

    The count and enumeration tests are only as good as the text handed to them,
    and the FIRST version of ``_prose_of`` read the module docstring alone — a
    reviewer put a stale count in a function docstring and both tests stayed
    green. This drives the real extractor over a real file containing all four
    prose sites, and asserts it still refuses executable text.

    Turns red by: reverting ``_prose_of`` to
    ``ast.get_docstring(ast.parse(source))`` (the class and function docstrings
    vanish), or by dropping the ``tokenize`` comment pass.
    """
    module = tmp_path / "sample.py"
    module.write_text(
        '"""MODULE_DOCSTRING five sources."""\n'
        "# COMMENT five sources\n"
        "\n"
        "class Thing:\n"
        '    """CLASS_DOCSTRING five sources."""\n'
        "\n"
        "    def method(self) -> None:\n"
        '        """METHOD_DOCSTRING five sources."""\n'
        "\n"
        "def top_level() -> str:\n"
        '    """FUNCTION_DOCSTRING five sources."""\n'
        '    return "EXECUTABLE_LITERAL five sources"\n',
        encoding="utf-8",
    )
    prose = _prose_of(module)
    for marker in (
        "MODULE_DOCSTRING",
        "COMMENT",
        "CLASS_DOCSTRING",
        "METHOD_DOCSTRING",
        "FUNCTION_DOCSTRING",
    ):
        assert marker in prose, f"_prose_of missed the {marker} site"
    # Executable text stays OUT, or the enumeration check would be satisfied by
    # the ``SourceSpec(...)`` literals it exists to compare against.
    assert "EXECUTABLE_LITERAL" not in prose
    # And the detector really sees every one of them through the extractor.
    assert len(_corpus_counts_claimed_in(prose)) == 5


def test_the_extractor_reads_markdown_whole(tmp_path: pathlib.Path) -> None:
    """A ``.md`` file has no executable text, so all of it is prose.

    Turns red by: making ``_prose_of`` run the Python branch on every path —
    ``tokenize`` raises on markdown, so this fails loudly rather than silently
    returning nothing.
    """
    doc = tmp_path / "runbook.md"
    doc.write_text("The corpus is five documents, one chunk each.\n", encoding="utf-8")
    assert _corpus_counts_claimed_in(_prose_of(doc)) == ["five documents"]
