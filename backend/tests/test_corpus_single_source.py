"""Drift guards: the shipped corpus is the single source of truth (#178).

Corpus content used to live in four places — the worker sources, the conftest
test fixture, ``db/seed/seed_catalog.py``, and the frontend offline KB — and only
the first was authoritative. Nothing tested that they agreed, which is exactly
how #170's Claude Code installation content reached ``main`` in one place and not
the others.

``db/seed`` is gone as a copy: it now ingests ``backend/app/worker/sources/*.md``
(see ``test_seed_and_pg_eval_guards.py``). The remaining copies cannot be
derived, for reasons that are deliberate, not accidental:

* ``tests.conftest.seed_catalog`` is a hand-abridged 6-chunk fixture. The whole
  golden/eval suite is anchored to it (``tests/eval/golden.jsonl``), and it is
  tuned to keep the hermetic keyword arm deterministic on SQLite where the vector
  arm is off. Deriving it from the real corpus would swap a 6-chunk fixture for a
  40-chunk one and move every retrieval number for reasons unrelated to the
  change being tested.
* The frontend offline KB is TypeScript and cannot import Python.

So instead of deriving them, this module makes drift *fail the build*: every
verbatim claim the fixture makes about a product must still be present in that
product's shipped source doc. Edit ``claude_code.md``'s install command without
updating the fixture and this test goes red — the failure mode #178 describes.
"""

from __future__ import annotations

import re

import pytest

from app.worker.allowlist import MVP_SOURCES, SourceSpec
from app.worker.fetchers import build_fetcher
from tests.conftest import corpus_fixture_specs

# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------
#
# A "claim" is a verbatim, checkable string — a shell command, a CLI flag, an
# environment variable, an HTTP header. Deliberately NOT prose: the fixture
# paraphrases the corpus on purpose, so requiring sentence-level agreement would
# be a false-positive machine. Commands and identifiers are where a stale copy
# actually hurts, because a user pastes them.

# ``(?<!\w)`` / ``(?!\w)`` keep an APOSTROPHE from opening a quote: in
# "the project's settings file" the ``'s`` would otherwise shift the quote parity
# for the rest of the string, and every real command after it would be extracted
# as the prose BETWEEN two commands (" and diagnose it with ") — claims that are
# never in the corpus, so the guard would fail permanently and get deleted.
_QUOTED_RE = re.compile(r"(?<!\w)'([^'\n]{3,120}?)'(?!\w)")  # 'npm install -g @openai/codex'
_ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")  # OPENAI_API_KEY
_FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]{1,40}")  # --model
_HEADER_RE = re.compile(r"\bx-[a-z0-9-]+\b")  # x-goog-api-key


def _normalize(text: str) -> str:
    """Collapse whitespace so a claim still matches across a hard-wrapped line.

    The source markdown wraps at ~88 columns, so ``'curl -fsSL
    https://claude.ai/install.sh | bash'`` is split mid-command on disk while the
    fixture holds it on one line. Without this the guard would report drift for
    every long command — a false positive that would get it deleted.
    """
    return " ".join(text.split())


def extract_claims(text: str) -> set[str]:
    """Return the verbatim, checkable claims in ``text``, whitespace-normalized.

    Normalizing FIRST is what lets a quoted command survive a hard line wrap;
    ``_QUOTED_RE`` deliberately refuses to span a newline, because a stray
    unbalanced quote would otherwise swallow half the document as one "claim".
    """
    normalized = _normalize(text)
    claims: set[str] = set()
    for pattern in (_QUOTED_RE, _ENV_VAR_RE, _FLAG_RE, _HEADER_RE):
        claims.update(pattern.findall(normalized))
    return claims


def _source_text(spec: SourceSpec) -> str:
    return _normalize(build_fetcher(spec).fetch(spec))


# A claim must appear as a WHOLE token run, not as a prefix of a longer one.
# Found by mutation-testing this guard: renaming the fixture's install command to
# ``@anthropic-ai/claude-code-next`` still "matched", because the corpus mentions
# ``@anthropic-ai/claude-code@latest`` elsewhere and a plain substring test found
# the shorter string inside it. A guard that a real drift walks straight through
# is worse than no guard, because it reads as coverage.
# ``.`` is deliberately NOT a continuation char: a command at the end of a
# sentence is followed by a full stop, and treating that as part of a longer
# token would reject every claim that ends a sentence.
_CONTINUATION = r"[\w@/-]"


def _present_in(claim: str, source_text: str) -> bool:
    """True if ``claim`` appears in ``source_text`` as a complete token run."""
    pattern = rf"(?<!{_CONTINUATION}){re.escape(claim)}(?!{_CONTINUATION})"
    return re.search(pattern, source_text) is not None


_SOURCES_BY_AREA = {spec.product_area: spec for spec in MVP_SOURCES}


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_fixture_covers_exactly_the_shipped_sources() -> None:
    """The fixture and the allowlist describe the same corpus, 1:1.

    Adding a source without seeding the fixture (or vice versa) means the
    hermetic suite tests a corpus that does not ship.
    """
    fixture_areas = {spec["product_area"] for spec in corpus_fixture_specs()}
    assert fixture_areas == set(_SOURCES_BY_AREA)

    by_area = {spec["product_area"]: spec for spec in corpus_fixture_specs()}
    for area, source in _SOURCES_BY_AREA.items():
        assert by_area[area]["source_name"] == source.name, area


@pytest.mark.parametrize("spec", corpus_fixture_specs(), ids=lambda s: str(s["product_area"]))
def test_fixture_claims_are_still_in_the_shipped_corpus(spec: dict[str, str]) -> None:
    """Regression guard for the #170/#178 failure class.

    Every command, flag, env var and header the hermetic fixture puts in front of
    the retriever must still exist in the doc that actually ships. If a corpus
    correction changes one and the fixture is not updated, the fixture is
    asserting against content no user can reach — and the eval suite would go on
    reporting green for an answer the live index can no longer support.
    """
    source_text = _source_text(_SOURCES_BY_AREA[spec["product_area"]])
    missing = sorted(
        claim for claim in extract_claims(spec["chunk_text"]) if not _present_in(claim, source_text)
    )
    assert not missing, (
        f"{spec['product_area']}: the conftest fixture claims {missing}, which is no longer "
        f"in {_SOURCES_BY_AREA[spec['product_area']].location}. Update the fixture (and the "
        "frontend KB) to match the corpus, or revert the corpus edit."
    )


# ---------------------------------------------------------------------------
# The guard can actually fail (a green guard that cannot go red is worthless)
# ---------------------------------------------------------------------------


def test_guard_detects_a_fixture_claim_the_corpus_does_not_make() -> None:
    """Failure path: a stale command in the fixture is caught."""
    source_text = _source_text(_SOURCES_BY_AREA["claude_code"])
    stale = "Install with 'npm install -g @anthropic-ai/claude-code-legacy'."
    missing = [c for c in extract_claims(stale) if not _present_in(c, source_text)]
    assert missing == ["npm install -g @anthropic-ai/claude-code-legacy"]


def test_guard_tolerates_a_command_hard_wrapped_in_the_markdown() -> None:
    """Edge case: a command split across a line wrap is present, not drifted.

    The source markdown wraps at ~88 columns, so a long command can land with a
    newline in the middle at any time an editor reflows a paragraph. Without the
    normalizer that reads as drift for a command that is genuinely there — a
    false positive, which is how a guard gets deleted instead of fixed. Synthetic
    rather than pinned to a currently-wrapped line, so re-wrapping the corpus
    cannot make this test vacuous.
    """
    wrapped = "run 'npm install -g\n@anthropic-ai/claude-code' to install it"
    claim = "npm install -g @anthropic-ai/claude-code"
    assert claim not in wrapped
    assert claim in _normalize(wrapped)
    # ...and the extractor recovers the claim from the wrapped text too.
    assert claim in extract_claims(wrapped)


def test_apostrophes_in_prose_do_not_fabricate_claims() -> None:
    """Edge case: possessives must not shift quote parity into bogus claims.

    The fixture says "the project's settings file" a few words before a quoted
    install command. A naive ``'(...)'`` match pairs the possessive apostrophe
    with the command's opening quote and extracts the PROSE between them.
    """
    text = "the project's settings file. Install with 'npm install -g @openai/codex' today"
    assert extract_claims(text) == {"npm install -g @openai/codex"}
