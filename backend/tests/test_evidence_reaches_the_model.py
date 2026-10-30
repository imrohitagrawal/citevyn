"""The model sees each shipped evidence chunk whole (#552).

Production refused "How do I update Claude Code?" although the shipped
``claude_code.md`` answers it in its Installation section. The chunker makes one
chunk per ``## `` section, and the answer generator cut every chunk to a fixed
number of characters before it reached the model, so the update commands near
the end of that section never did.

These tests read the REAL shipped files through the real fetcher, parser and
chunker (the same path the ingest worker runs), so a corpus edit that grows a
section past the cap is caught here, not in production.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.answer.generate import AnswerGenerator, build_user_prompt
from app.llm.types import LLMResult
from app.retrieval.types import EvidenceHit
from app.worker.allowlist import MVP_SOURCES, SourceSpec, get_source
from app.worker.chunker import ChunkDraft, chunk_document
from app.worker.fetchers import LocalFetcher
from app.worker.parser import parse_markdown

_BACKEND = Path(__file__).resolve().parents[1]
_SOURCES_DIR = _BACKEND / "app" / "worker" / "sources"


def _chunks(source: SourceSpec) -> list[ChunkDraft]:
    """Chunk ``source`` exactly as the ingest worker does."""
    raw = LocalFetcher().fetch(source)
    return chunk_document(parse_markdown(raw), source=source)


def _hit(*, source_name: str, title: str, chunk_text: str) -> EvidenceHit:
    """An evidence hit carrying ``chunk_text`` as the stored chunk text."""
    return EvidenceHit(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        source_name=source_name,
        product_area=source_name,
        document_title=title,
        section_path="s",
        heading="h",
        context_summary="c",
        source_url=f"https://docs.example.com/{source_name}",
        chunk_text=chunk_text,
        score=1.0,
        rank=1,
    )


def _shipped_specs() -> list[SourceSpec]:
    """One spec per shipped ``sources/*.md`` file.

    Walks the folder rather than the allowlist so a file added there is checked
    too. A listed file uses its real spec; an unlisted one gets a plain spec
    pointing at the same bytes.
    """
    by_location = {spec.location: spec for spec in MVP_SOURCES}
    specs: list[SourceSpec] = []
    for path in sorted(_SOURCES_DIR.glob("*.md")):
        location = path.relative_to(_BACKEND).as_posix()
        specs.append(
            by_location.get(location)
            or SourceSpec(
                name=path.stem,
                product_area=path.stem,
                title=path.stem,
                fetcher="local",
                location=location,
            )
        )
    return specs


class _CapturingLLM:
    """Records the user prompt the generator sends; never calls a provider."""

    def __init__(self) -> None:
        self.users: list[str] = []

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> LLMResult:
        self.users.append(user)
        return LLMResult(text="ok", input_tokens=0, output_tokens=0, model="m", provider="stub")


async def test_update_commands_in_claude_code_installation_reach_the_model() -> None:
    """The prompt sent for "How do I update Claude Code?" carries the update commands.

    Red when: the evidence cap in app/answer/generate.py is shorter than the
    shipped claude_code.md Installation chunk (it is 400 today; the chunk is
    about 1,300 characters and "claude update" starts near character 940), or
    that section stops naming "claude update" and "brew upgrade".
    """
    spec = get_source("claude_code")
    installation = [c for c in _chunks(spec) if c.heading == "Installation"]
    assert len(installation) == 1, "claude_code.md must have exactly one '## Installation'"
    chunk = installation[0]
    # Partner: the source itself still says it, so a miss below is the cap's fault.
    assert "claude update" in chunk.text
    assert "brew upgrade" in chunk.text

    llm = _CapturingLLM()
    generator = AnswerGenerator(llm, max_tokens=512, temperature=0.0)
    hit = _hit(source_name=spec.name, title=spec.title, chunk_text=chunk.text)
    await generator.generate("How do I update Claude Code?", [hit])

    assert len(llm.users) == 1
    prompt = llm.users[0]
    assert "claude update" in prompt, prompt
    assert "brew upgrade" in prompt, prompt


def test_every_shipped_chunk_fits_whole_in_the_prompt() -> None:
    """No shipped section is cut before the model sees it.

    Red when: a shipped app/worker/sources/*.md section grows past the evidence
    cap in app/answer/generate.py, or the cap is lowered below the longest
    shipped chunk.
    """
    specs = _shipped_specs()
    checked = 0
    cut: list[str] = []
    for spec in specs:
        for chunk in _chunks(spec):
            checked += 1
            hit = _hit(source_name=spec.name, title=spec.title, chunk_text=chunk.text)
            prompt = build_user_prompt("q?", [hit])
            if chunk.text[-40:] not in prompt or chunk.text not in prompt:
                cut.append(f"{spec.location} :: {chunk.heading!r} ({len(chunk.text)} chars)")
    # Partner: the population is real, so an empty ``cut`` means something.
    assert len(specs) >= len(MVP_SOURCES), [s.location for s in specs]
    assert checked >= len(specs), checked
    assert not cut, f"{len(cut)} of {checked} shipped chunks are cut:\n" + "\n".join(cut)


def test_a_huge_evidence_text_is_still_cut() -> None:
    """The cap still exists, so the prompt stays bounded.

    Partner to the test above: that one would pass if truncation were simply
    deleted. This one passes today and must keep passing after the fix.

    Red when: the evidence cap in app/answer/generate.py is removed, or raised
    past 10,000 characters per chunk.
    """
    words = " ".join(f"word{i}" for i in range(4000))
    text = f"HEAD-SENTINEL {words} TAIL-SENTINEL"
    assert len(text) > 20_000
    # Far longer than any shipped chunk, so no fix sized to the corpus keeps it.
    longest_shipped = max(len(c.text) for s in _shipped_specs() for c in _chunks(s))
    assert len(text) > 10 * longest_shipped

    prompt = build_user_prompt("q?", [_hit(source_name="x", title="X", chunk_text=text)])

    assert "HEAD-SENTINEL" in prompt  # the snippet is kept, not emptied
    assert "TAIL-SENTINEL" not in prompt
    assert len(prompt) < 10_000, len(prompt)
