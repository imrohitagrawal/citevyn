"""Content digests of the shipped corpus, pinned so a corpus EDIT is reviewable.

Why a digest and not more claim-matching
----------------------------------------
``test_corpus_single_source.py`` checks that every verbatim claim the downstream
copies make is still present in the shipped corpus. That guard is
**one-directional**: it catches the corpus *losing* or *renaming* something the
copies still say. It cannot catch the corpus *gaining* content — which is
exactly the #170 shape. ``claude_code.md`` had no installation section at all;
the fix added one there and to the conftest fixture, but not to
``db/seed/seed_catalog.py`` or the frontend KB, and a fresh ``make demo`` stack
went on refusing "How do I install Claude Code?". Every claim-subset guard in
the repo was green throughout, because a copy that says LESS never contradicts
the corpus.

The gaining direction cannot be checked by containment: the shipped corpus is
deliberately much larger than its copies (the conftest fixture is a 6-chunk
abridgement; the frontend KB is marketing-length prose). Requiring the copies to
restate the whole corpus would be requiring them not to be abridgements.

So the check is a **review checkpoint** instead: pin a digest per source, and
fail when it moves. Any edit to an authoritative doc — one added sentence
included — turns red until a human has looked at the downstream copies and
re-pinned. That is the only signal that is symmetric in principle: it does not
care whether content arrived or left.

Regenerate after reviewing the copies::

    cd backend && uv run python -m tests.corpus_mirror --write

Whitespace is collapsed before hashing, so re-wrapping a paragraph (the source
markdown is hard-wrapped at ~88 columns and editors reflow it) does not trip the
guard. A word change does.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.worker.allowlist import MVP_SOURCES, SourceSpec
from app.worker.fetchers import build_fetcher

MANIFEST_PATH = Path(__file__).resolve().parent / "corpus_mirror_manifest.json"

# The copies a corpus edit has to be reconciled against. Named here (not only in
# prose) so the failure message tells a contributor exactly where to look — a
# guard whose message is "hash changed" gets re-pinned without the review that is
# the entire point of it.
MIRRORS: tuple[str, ...] = (
    "backend/tests/conftest.py::_CORPUS_FIXTURE_SPECS (the hermetic 6-chunk fixture)",
    "backend/tests/eval/golden.jsonl (cases anchored to that fixture)",
    "frontend/src/data/knowledgeBase.ts (the offline/demo KB)",
)


def _normalize(text: str) -> str:
    """Collapse whitespace — a reflow is not a content change."""
    return " ".join(text.split())


def source_digest(text: str) -> str:
    """``sha256:<hex>`` over the whitespace-normalized text of one source."""
    return f"sha256:{hashlib.sha256(_normalize(text).encode('utf-8')).hexdigest()}"


def read_source(spec: SourceSpec) -> str:
    return build_fetcher(spec).fetch(spec)


def compute_digests() -> dict[str, str]:
    """Digest every allowlisted source, keyed by source name."""
    return {spec.name: source_digest(read_source(spec)) for spec in sorted(MVP_SOURCES, key=_key)}


def _key(spec: SourceSpec) -> str:
    return spec.name


def load_manifest() -> dict[str, str]:
    """The pinned digests. Missing file ⇒ empty, so the guard reports every source."""
    if not MANIFEST_PATH.exists():
        return {}
    data: dict[str, str] = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["sources"]
    return data


def write_manifest(digests: dict[str, str]) -> None:
    """Re-pin the manifest (the ``--write`` path; never called from a test)."""
    payload = {
        "_comment": (
            "Pinned digests of backend/app/worker/sources/*.md. Regenerate with "
            "`cd backend && uv run python -m tests.corpus_mirror --write` ONLY after "
            "reconciling the downstream copies listed in tests/corpus_mirror.py."
        ),
        "sources": dict(sorted(digests.items())),
    }
    MANIFEST_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:  # pragma: no cover - developer tool, not part of the suite
    import sys

    if "--write" in sys.argv:
        write_manifest(compute_digests())
        print(f"re-pinned {MANIFEST_PATH}")
        return 0
    print(json.dumps(compute_digests(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
