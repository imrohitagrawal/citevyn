"""#601: the shipped-corpus recorder, with fake embedders and temporary files.

Every test here is free: no provider, no database. The real recording is a separate step.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable
from typing import Any

import pytest

from app.core.config import get_settings
from app.embeddings.errors import EmbedderUnavailable
from app.embeddings.factory import EmbedderIdentity
from tests.eval import record_vectors
from tests.eval.recorded import (
    ChunkVector,
    ShippedEvalError,
    load_chunk_vectors,
    load_question_vectors,
    sha256_text,
)
from tests.eval.shipped import all_shipped_cases, known_question_texts

IDENTITY = EmbedderIdentity("gemini", "gemini-embedding-001", 4)
HEADER = record_vectors.question_header(IDENTITY)
CHUNK_HEADER = record_vectors.chunk_header(IDENTITY, "sha256:" + "a" * 64)
VECTOR = [1.0, 0.0, 0.0, 0.0]
FOLLOW_UP_TEXT = "What is the rate limit for the Claude API? How can I raise it?"


class FakeEmbedder:
    """Counts calls, and fails on the numbered call when told to."""

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.queries: list[str] = []
        self.documents: list[str] = []
        self._fail_on_call = fail_on_call

    @property
    def dim(self) -> int:
        return 4

    def _maybe_fail(self) -> None:
        if len(self.queries) + len(self.documents) + 1 == self._fail_on_call:
            raise EmbedderUnavailable("provider down")

    async def embed(self, text: str) -> list[float]:
        self._maybe_fail()
        self.queries.append(text)
        return list(VECTOR)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._maybe_fail()
        self.documents.extend(texts)
        return [list(VECTOR) for _ in texts]


def _no_provider(*_: Any, **__: Any) -> Any:
    pytest.fail("this run must not build an embedder")


@pytest.fixture
def recording_paths(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[pathlib.Path, pathlib.Path]:
    questions = tmp_path / "recorded" / "questions.jsonl"
    chunks = tmp_path / "recorded" / "chunks.jsonl"
    monkeypatch.setattr(record_vectors, "RECORDED_QUESTIONS_PATH", questions)
    monkeypatch.setattr(record_vectors, "RECORDED_CHUNKS_PATH", chunks)
    monkeypatch.setattr(record_vectors, "build_embedder", _no_provider)
    monkeypatch.setattr(record_vectors, "metered_embedder", _no_provider)
    return questions, chunks


def test_dry_run_builds_no_embedder_and_lists_every_text(
    recording_paths: tuple[pathlib.Path, pathlib.Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: ``--dry-run`` reaches for a provider, or stops listing the real texts."""
    questions, chunks = recording_paths
    assert record_vectors.main(["--dry-run"]) == 0
    output = capsys.readouterr().out
    assert f"distinct question texts: {len(known_question_texts())}\n" in output
    assert len(known_question_texts()) >= 120
    assert json.dumps(FOLLOW_UP_TEXT) in output
    assert not questions.exists()
    assert not chunks.exists()


async def test_the_capture_runs_the_real_path_and_matches_the_known_texts() -> None:
    """Red when: the eval's text list drifts from what the real question path embeds."""
    captured = await record_vectors.capture_question_texts(all_shipped_cases(), get_settings())
    # Partner: the rewritten follow-up proves the path ran, not a copy of the questions.
    assert FOLLOW_UP_TEXT in captured
    assert "How can I raise it?" not in captured
    assert set(captured) == known_question_texts()


async def test_resume_does_not_embed_what_was_already_recorded(tmp_path: pathlib.Path) -> None:
    """Red when: a re-run after a crash pays again for texts already on disk."""
    path = tmp_path / "q.jsonl"
    first = FakeEmbedder(fail_on_call=3)
    with pytest.raises(EmbedderUnavailable):
        await record_vectors.record_questions(
            path, ["a", "b", "c", "d"], embedder=first, header=HEADER
        )
    assert first.queries == ["a", "b"]
    second = FakeEmbedder()
    done = await record_vectors.record_questions(
        path, ["a", "b", "c", "d"], embedder=second, header=HEADER
    )
    assert (done, second.queries) == (2, ["c", "d"])
    assert set(load_question_vectors(path).rows) == {sha256_text(t) for t in "abcd"}


async def test_a_failure_stops_the_run_and_keeps_earlier_work(tmp_path: pathlib.Path) -> None:
    """Red when: an embedding failure skips the row and the run carries on."""
    path = tmp_path / "q.jsonl"
    embedder = FakeEmbedder(fail_on_call=2)
    with pytest.raises(EmbedderUnavailable):
        await record_vectors.record_questions(
            path, ["a", "b", "c"], embedder=embedder, header=HEADER
        )
    assert embedder.queries == ["a"]
    assert set(load_question_vectors(path).rows) == {sha256_text("a")}


def test_the_command_reports_a_failure_with_a_non_zero_exit(
    recording_paths: tuple[pathlib.Path, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Red when: a stopped recording exits 0 and looks finished."""

    async def stopped(_: Any) -> int:
        raise ShippedEvalError("stopped on purpose")

    monkeypatch.setattr(record_vectors, "_run", stopped)
    assert record_vectors.main([]) == 1
    assert "stopped on purpose" in capsys.readouterr().err


async def test_the_lines_are_sorted_and_stable_whatever_the_input_order(
    tmp_path: pathlib.Path,
) -> None:
    """Red when: file order follows the run, so a re-record makes a noisy diff."""
    one, two = tmp_path / "1.jsonl", tmp_path / "2.jsonl"
    await record_vectors.record_questions(
        one, ["b", "a", "c"], embedder=FakeEmbedder(), header=HEADER
    )
    await record_vectors.record_questions(
        two, ["c", "b", "a"], embedder=FakeEmbedder(), header=HEADER
    )
    assert one.read_bytes() == two.read_bytes()
    keys = [json.loads(line)["text_sha256"] for line in one.read_text().splitlines()[1:]]
    assert keys == sorted(keys)
    assert len(keys) == 3


async def test_a_text_already_recorded_spends_nothing(tmp_path: pathlib.Path) -> None:
    """Red when: an up-to-date recording still calls the provider."""
    path = tmp_path / "q.jsonl"
    await record_vectors.record_questions(path, ["a"], embedder=FakeEmbedder(), header=HEADER)
    before = path.read_bytes()
    second = FakeEmbedder()
    assert await record_vectors.record_questions(path, ["a"], embedder=second, header=HEADER) == 0
    assert second.queries == []
    assert path.read_bytes() == before


async def test_a_recording_from_another_model_is_refused_before_any_call(
    tmp_path: pathlib.Path,
) -> None:
    """Red when: two vector spaces can end up in one file."""
    path = tmp_path / "q.jsonl"
    await record_vectors.record_questions(path, ["a"], embedder=FakeEmbedder(), header=HEADER)
    other = FakeEmbedder()
    with pytest.raises(ShippedEvalError, match="model"):
        await record_vectors.record_questions(
            path, ["b"], embedder=other, header={**HEADER, "model": "another-model"}
        )
    assert other.queries == []


def _chunk_rows(keys: Iterable[str], texts: dict[str, str]) -> dict[str, ChunkVector]:
    return {key: ChunkVector(sha256_text(texts[key]), list(VECTOR)) for key in keys}


async def test_only_changed_chunks_are_embedded_and_removed_ones_leave(
    tmp_path: pathlib.Path,
) -> None:
    """Red when: a one-passage edit re-embeds the corpus, or a dropped heading stays."""
    path = tmp_path / "c.jsonl"
    old = {"s#same": "same text", "s#edited": "old text", "s#removed": "gone"}
    await record_vectors.record_chunks(
        path,
        old,
        header=CHUNK_HEADER,
        exported=_chunk_rows(old, old),
        embedder=None,
    )
    new = {"s#same": "same text", "s#edited": "new text", "s#added": "added text"}
    embedder = FakeEmbedder()
    done = await record_vectors.record_chunks(
        path, new, header=CHUNK_HEADER, exported={}, embedder=embedder
    )
    assert done == 2
    assert sorted(embedder.documents) == ["added text", "new text"]
    recorded = load_chunk_vectors(path)
    assert set(recorded.rows) == set(new)
    assert recorded.rows["s#edited"].content_checksum == sha256_text("new text")


async def test_a_changed_chunk_needs_the_explicit_flag_and_is_named(tmp_path: pathlib.Path) -> None:
    """Red when: a source edit can spend on chunks without asking, or names nothing."""
    path = tmp_path / "c.jsonl"
    texts = {"s#one": "one text", "s#two": "two text"}
    with pytest.raises(ShippedEvalError, match="s#two") as caught:
        await record_vectors.record_chunks(
            path,
            texts,
            header=CHUNK_HEADER,
            exported=_chunk_rows(["s#one"], texts),
            embedder=None,
        )
    assert "s#one" not in str(caught.value)
    assert "--embed-changed-chunks" in str(caught.value)
    # Partner: the free export was kept, so the paid re-run has less to do.
    assert set(load_chunk_vectors(path).rows) == {"s#one"}


async def test_an_exported_vector_for_old_text_is_not_trusted(tmp_path: pathlib.Path) -> None:
    """Red when: an index built from an older source can pose as the current text."""
    path = tmp_path / "c.jsonl"
    texts = {"s#one": "current text"}
    exported = {"s#one": ChunkVector(sha256_text("older text"), list(VECTOR))}
    with pytest.raises(ShippedEvalError, match="s#one"):
        await record_vectors.record_chunks(
            path, texts, header=CHUNK_HEADER, exported=exported, embedder=None
        )


async def test_chunk_embedding_resumes_after_a_crash(tmp_path: pathlib.Path) -> None:
    """Red when: a failure in the chunk step loses the vectors already paid for."""
    path = tmp_path / "c.jsonl"
    texts = {"s#a": "a text", "s#b": "b text"}
    with pytest.raises(EmbedderUnavailable):
        await record_vectors.record_chunks(
            path,
            texts,
            header=CHUNK_HEADER,
            exported={},
            embedder=FakeEmbedder(fail_on_call=2),
        )
    assert set(load_chunk_vectors(path).rows) == {"s#a"}
    second = FakeEmbedder()
    await record_vectors.record_chunks(
        path, texts, header=CHUNK_HEADER, exported={}, embedder=second
    )
    assert second.documents == ["b text"]


async def test_a_vector_of_the_wrong_length_stops_the_recording(tmp_path: pathlib.Path) -> None:
    """Red when: a provider returning the wrong dimension is written to the fixture."""
    path = tmp_path / "q.jsonl"

    class WrongDim(FakeEmbedder):
        async def embed(self, text: str) -> list[float]:
            return [1.0]

    with pytest.raises(ShippedEvalError, match="expected 4"):
        await record_vectors.record_questions(path, ["a"], embedder=WrongDim(), header=HEADER)
    assert not path.exists()


async def test_the_export_refuses_a_database_that_is_not_local() -> None:
    """Red when: the free export can be pointed at a hosted database."""
    with pytest.raises(ShippedEvalError, match="local"):
        await record_vectors.export_chunk_vectors(
            "postgresql+psycopg://reader:hidden@db.example.com:5432/app", identity=IDENTITY
        )


def test_a_real_run_refuses_the_stub_embedder(
    recording_paths: tuple[pathlib.Path, pathlib.Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: hash-bucket vectors could be recorded as if they were real."""
    assert get_settings().embedding_provider == "stub"
    assert record_vectors.main([]) == 1
    assert "stub" in capsys.readouterr().err
    questions, chunks = recording_paths
    assert not questions.exists()
    assert not chunks.exists()
