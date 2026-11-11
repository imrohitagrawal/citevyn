"""#601: the shipped-corpus eval's loader, stamps, labels and report (free default job).

Written by the test designer before ``tests/eval/shipped.py`` exists, so these name
its contract. Every test imports the module through :func:`_shipped` rather than at
the top of the file: a missing module then fails each test by itself instead of
one collection error hiding the count.

The contract, in one place:

``ShippedEvalError(RuntimeError)``
    Every refusal. ``RecordingMismatch`` and ``MissingRecordedVector`` subclass it;
    neither is an ``EmbedderUnavailable``.
``RecordingMismatch``
    ``.changed``, ``.missing``, ``.stale``: sorted ``source#heading`` lists.
``ShippedLabelError(ValueError)``
    A label file that breaks the rules; the message names the row or key.
``RECORD_COMMAND``
    The command to re-record, printed in every recording failure.
``shipped_heading_keys() -> dict[str, str]``
    ``source#heading`` to content checksum, from the real chunker over ``MVP_SOURCES``.
``load_chunk_vectors(path)``, ``load_question_vectors(path)``
    Each returns an object with ``.header`` (mapping) and ``.rows`` (mapping). The first
    line of a file is its header: ``schema``, ``provider``, ``model``, ``dim``,
    ``task_type``, and ``corpus_hash`` on the chunk file. Chunk rows carry ``chunk``,
    ``content_checksum`` and ``vector`` (base64 float32), and load to objects with
    ``.content_checksum`` and ``.vector``. Question rows carry ``text_sha256`` and
    ``vector``, and load to a plain ``list[float]``.
``check_chunk_vectors(recorded, current) -> int``
    Compares per chunk; returns how many matched, or raises ``RecordingMismatch``.
``identity_from_header(header) -> EmbedderIdentity``
``RecordedEmbedder(chunks=..., questions=...)``
    An ``Embedder``, with ``.index_identity`` and ``.query_identity``.
``shipped_corpus_session(settings, query_embedder, query_identity)``
``load_golden_labels(path, heading_keys)``, ``load_shipped_questions(path, heading_keys)``
    Each returns ``ShippedCase`` objects: ``id``, ``question``, ``origin`` (``golden`` or
    ``coverage``), ``kind``, ``coverage_kind``, ``shipped_gold``, ``follow_up``,
    ``expected_empty``, ``facet``, ``planned_for``.
``known_question_texts() -> set[str]``
    Every exact text the recording must cover.
``ShippedOutcome(case_id, origin, kind, coverage_kind, gold, retrieved, route, winning_arm,
routed_domain, degrade, follow_up)``
``build_report(outcomes) -> dict``, ``render_summary(report) -> str``,
``write_report(report, json_path, summary_path)``

The real recording does not exist yet, so the two tests that read it stay red until the
recording is committed.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import pathlib
import struct
from collections.abc import Iterable, Mapping
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.embeddings import Embedder
from app.embeddings.errors import EmbedderUnavailable
from app.embeddings.factory import EmbedderIdentity
from app.retrieval.hybrid import HybridRetriever
from app.retrieval.types import VectorDegrade
from app.worker.allowlist import MVP_SOURCES
from app.worker.chunker import chunk_document
from app.worker.parser import parse_markdown

BACKEND = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent
EVAL_DIR = REPO_ROOT / "tests" / "eval"
GOLDEN = EVAL_DIR / "golden.jsonl"
QUESTIONS = EVAL_DIR / "shipped_questions.jsonl"
RECORDED_CHUNKS = EVAL_DIR / "recorded" / "shipped_chunk_vectors.jsonl"
RECORDED_QUESTIONS = EVAL_DIR / "recorded" / "question_vectors.jsonl"

SHIPPED_PASSAGES = 44
GOLDEN_ANSWERABLE = 44
GOLDEN_REFUSALS = 19
COVERAGE_KINDS = {
    "exact",
    "entity",
    "keyword",
    "vector_scope",
    "semantic",
    "hybrid",
    "multi_product",
}
# The code tokens #370's design table says keyword search must handle.
PLANNED_370_TOKENS = (
    "--model",
    "CLAUDE.md",
    "x-goog-api-key",
    "OPENAI_API_KEY",
    "streamGenerateContent",
    "generate-content",
    "/compact",
    "DISABLE_AUTOUPDATER",
    "claude update",
)

CHUNK_HEADER: dict[str, Any] = {
    "schema": 1,
    "provider": "gemini",
    "model": "gemini-embedding-001",
    "dim": 4,
    "task_type": "RETRIEVAL_DOCUMENT",
    "corpus_hash": "sha256:" + "a" * 64,
}
QUESTION_HEADER: dict[str, Any] = {
    "schema": 1,
    "provider": "gemini",
    "model": "gemini-embedding-001",
    "dim": 4,
    "task_type": "RETRIEVAL_QUERY",
}


def _shipped() -> ModuleType:
    """The module under test. It does not exist until the builder writes it."""
    return importlib.import_module("tests.eval.shipped")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_vector(vector: Iterable[float]) -> str:
    values = list(vector)
    return base64.b64encode(struct.pack(f"<{len(values)}f", *values)).decode("ascii")


def write_jsonl(path: pathlib.Path, header: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]):
    lines = [json.dumps(header), *(json.dumps(row) for row in rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_chunk_file(
    path: pathlib.Path,
    vectors: Mapping[str, tuple[str, list[float]]],
    header: Mapping[str, Any] | None = None,
) -> pathlib.Path:
    """Write a chunk recording from ``{source#heading: (checksum, vector)}``."""
    return write_jsonl(
        path,
        header if header is not None else CHUNK_HEADER,
        (
            {"chunk": chunk, "content_checksum": checksum, "vector": encode_vector(vector)}
            for chunk, (checksum, vector) in sorted(vectors.items())
        ),
    )


def write_question_file(
    path: pathlib.Path,
    vectors: Mapping[str, list[float]],
    header: Mapping[str, Any] | None = None,
) -> pathlib.Path:
    """Write a question recording from ``{exact text: vector}``."""
    return write_jsonl(
        path,
        header if header is not None else QUESTION_HEADER,
        (
            {"text_sha256": sha256_text(text), "vector": encode_vector(vector)}
            for text, vector in sorted(vectors.items())
        ),
    )


@pytest.fixture
def chunk_file(tmp_path: pathlib.Path) -> pathlib.Path:
    return write_chunk_file(
        tmp_path / "chunks.jsonl",
        {
            "codex#CLI flags": ("c" * 64, [1.0, 0.0, 0.0, 0.0]),
            "codex#CLI sandbox": ("d" * 64, [0.0, 1.0, 0.0, 0.0]),
        },
    )


@pytest.fixture
def question_file(tmp_path: pathlib.Path) -> pathlib.Path:
    return write_question_file(
        tmp_path / "questions.jsonl", {"How do I sandbox Codex?": [0.0, 0.5, 0.5, 0.0]}
    )


def _recorded_embedder(chunk_file: pathlib.Path, question_file: pathlib.Path) -> Any:
    shipped = _shipped()
    return shipped.RecordedEmbedder(
        chunks=shipped.load_chunk_vectors(chunk_file),
        questions=shipped.load_question_vectors(question_file),
    )


# --- Loader ------------------------------------------------------------------------


def test_the_chunk_loader_decodes_the_header_and_float32_vectors(chunk_file) -> None:
    """Red when: the chunk loader misreads the header or the base64 float32 encoding."""
    recorded = _shipped().load_chunk_vectors(chunk_file)
    assert recorded.header["model"] == "gemini-embedding-001"
    assert set(recorded.rows) == {"codex#CLI flags", "codex#CLI sandbox"}
    assert recorded.rows["codex#CLI sandbox"].content_checksum == "d" * 64
    assert list(recorded.rows["codex#CLI sandbox"].vector) == [0.0, 1.0, 0.0, 0.0]


def test_the_question_loader_keys_rows_by_the_hash_of_the_exact_text(question_file) -> None:
    """Red when: question rows are keyed by anything but ``text_sha256``."""
    recorded = _shipped().load_question_vectors(question_file)
    assert list(recorded.rows) == [sha256_text("How do I sandbox Codex?")]
    assert list(recorded.rows[sha256_text("How do I sandbox Codex?")]) == [0.0, 0.5, 0.5, 0.0]


@pytest.mark.parametrize("field", ["schema", "provider", "model", "dim", "task_type"])
def test_a_header_missing_a_field_is_refused_by_name(tmp_path, field: str) -> None:
    """Red when: a required header field becomes optional in the loader."""
    header = {k: v for k, v in QUESTION_HEADER.items() if k != field}
    path = write_question_file(tmp_path / "q.jsonl", {"x": [0.0, 0.0, 0.0, 1.0]}, header=header)
    with pytest.raises(_shipped().ShippedEvalError, match=field):
        _shipped().load_question_vectors(path)


def test_a_vector_of_the_wrong_length_is_refused_and_names_the_row(tmp_path) -> None:
    """Red when: the loader stops comparing a row's length with the header's ``dim``."""
    path = write_chunk_file(tmp_path / "c.jsonl", {"codex#CLI flags": ("c" * 64, [1.0, 0.0])})
    with pytest.raises(_shipped().ShippedEvalError, match="codex#CLI flags"):
        _shipped().load_chunk_vectors(path)


# --- The per-chunk checksum --------------------------------------------------------


def test_a_matching_recording_passes_and_counts_every_chunk(chunk_file) -> None:
    """Red when: ``check_chunk_vectors`` stops counting, or rejects a clean recording."""
    shipped = _shipped()
    recorded = shipped.load_chunk_vectors(chunk_file)
    current = {"codex#CLI flags": "c" * 64, "codex#CLI sandbox": "d" * 64}
    assert shipped.check_chunk_vectors(recorded, current) == 2


def test_a_changed_chunk_is_named_with_the_re_record_command(chunk_file) -> None:
    """Red when: a mismatch stops naming exactly the changed chunk and the command."""
    shipped = _shipped()
    recorded = shipped.load_chunk_vectors(chunk_file)
    current = {"codex#CLI flags": "e" * 64, "codex#CLI sandbox": "d" * 64}
    with pytest.raises(shipped.RecordingMismatch) as caught:
        shipped.check_chunk_vectors(recorded, current)
    assert caught.value.changed == ["codex#CLI flags"]
    assert caught.value.missing == []
    assert caught.value.stale == []
    message = str(caught.value)
    assert "codex#CLI flags" in message
    assert "codex#CLI sandbox" not in message
    assert "record_vectors" in shipped.RECORD_COMMAND
    assert shipped.RECORD_COMMAND in message


def test_a_new_chunk_is_named_as_missing(chunk_file) -> None:
    """Red when: a heading added to a source can pass with no recorded vector."""
    shipped = _shipped()
    current = {
        "codex#CLI flags": "c" * 64,
        "codex#CLI sandbox": "d" * 64,
        "codex#CLI plugins": "f" * 64,
    }
    with pytest.raises(shipped.RecordingMismatch) as caught:
        shipped.check_chunk_vectors(shipped.load_chunk_vectors(chunk_file), current)
    assert caught.value.missing == ["codex#CLI plugins"]
    assert caught.value.changed == []


def test_a_removed_chunk_is_named_as_stale(chunk_file) -> None:
    """Red when: a vector for a deleted heading is silently carried in the recording."""
    shipped = _shipped()
    current = {"codex#CLI flags": "c" * 64}
    with pytest.raises(shipped.RecordingMismatch) as caught:
        shipped.check_chunk_vectors(shipped.load_chunk_vectors(chunk_file), current)
    assert caught.value.stale == ["codex#CLI sandbox"]


def test_a_differing_corpus_hash_alone_does_not_fail(tmp_path) -> None:
    """Red when: an all-or-nothing corpus check returns; the per-chunk checksum decides."""
    shipped = _shipped()
    header = {**CHUNK_HEADER, "corpus_hash": "sha256:" + "b" * 64}
    path = write_chunk_file(
        tmp_path / "c.jsonl", {"codex#CLI flags": ("c" * 64, [1.0, 0.0, 0.0, 0.0])}, header=header
    )
    assert (
        shipped.check_chunk_vectors(shipped.load_chunk_vectors(path), {"codex#CLI flags": "c" * 64})
        == 1
    )


def test_heading_keys_are_the_real_chunkers_44_passages_and_checksums() -> None:
    """Red when: the key or checksum stops being derived from the real chunker."""
    expected: dict[str, str] = {}
    for spec in MVP_SOURCES:
        parsed = parse_markdown((BACKEND / spec.location).read_text(encoding="utf-8"))
        for draft in chunk_document(parsed, source=spec):
            expected[f"{spec.name}#{draft.heading}"] = sha256_text(draft.text)
    assert len(expected) == SHIPPED_PASSAGES
    assert _shipped().shipped_heading_keys() == expected


def test_the_committed_recording_matches_the_shipped_corpus() -> None:
    """Red when: a source edit lands without re-recording its changed chunks."""
    shipped = _shipped()
    recorded = shipped.load_chunk_vectors(RECORDED_CHUNKS)
    assert shipped.check_chunk_vectors(recorded, shipped.shipped_heading_keys()) == SHIPPED_PASSAGES


def test_every_known_question_has_a_recorded_vector() -> None:
    """Red when: a question is added or reworded without recording its text."""
    shipped = _shipped()
    known = shipped.known_question_texts()
    # Partner to the subset check: the population must hold both question sets.
    assert "What is the rate limit for the Claude API?" in known
    assert "Where does Codex read OPENAI_API_KEY from?" in known
    assert len(known) >= 120
    recorded = shipped.load_question_vectors(RECORDED_QUESTIONS)
    absent = sorted(text for text in known if sha256_text(text) not in recorded.rows)
    assert absent == []


# --- RecordedEmbedder --------------------------------------------------------------


def test_the_recorded_embedder_is_an_embedder_and_returns_the_recorded_vector(
    chunk_file, question_file
) -> None:
    """Red when: ``RecordedEmbedder`` stops meeting the protocol retrieval depends on."""
    embedder = _recorded_embedder(chunk_file, question_file)
    assert isinstance(embedder, Embedder)
    assert embedder.dim == 4


async def test_a_recorded_question_returns_its_vector(chunk_file, question_file) -> None:
    """Red when: ``embed`` returns anything but the vector recorded for that exact text."""
    embedder = _recorded_embedder(chunk_file, question_file)
    assert await embedder.embed("How do I sandbox Codex?") == [0.0, 0.5, 0.5, 0.0]


async def test_documents_are_found_by_the_checksum_of_their_text(tmp_path, question_file) -> None:
    """Red when: ``embed_documents`` stops resolving a chunk's text by its checksum."""
    chunks = write_chunk_file(
        tmp_path / "c.jsonl",
        {"codex#CLI flags": (sha256_text("Codex — flags. body"), [1.0, 0.0, 0.0, 0.0])},
    )
    embedder = _recorded_embedder(chunks, question_file)
    assert await embedder.embed_documents(["Codex — flags. body"]) == [[1.0, 0.0, 0.0, 0.0]]


async def test_a_missing_question_raises_a_loud_error_naming_it(chunk_file, question_file) -> None:
    """Red when: an unrecorded question gets a fallback vector, or the error goes generic."""
    shipped = _shipped()
    embedder = _recorded_embedder(chunk_file, question_file)
    text = "A question nobody recorded"
    with pytest.raises(shipped.MissingRecordedVector) as caught:
        await embedder.embed(text)
    assert not isinstance(caught.value, EmbedderUnavailable)
    assert sha256_text(text) in str(caught.value)
    assert shipped.RECORD_COMMAND in str(caught.value)


async def test_a_missing_document_raises_a_loud_error(chunk_file, question_file) -> None:
    """Red when: an unrecorded chunk text is embedded as a fallback rather than refused."""
    shipped = _shipped()
    embedder = _recorded_embedder(chunk_file, question_file)
    with pytest.raises(shipped.MissingRecordedVector):
        await embedder.embed_documents(["Text that was never recorded"])


class _ArmThatEmbeds:
    """The vector arm's only use of the embedder, without a database."""

    def __init__(self, embedder: Any) -> None:
        self._embedder = embedder

    async def retrieve(self, question: str, **_: Any) -> list[object]:
        await self._embedder.embed(question)
        return []


class _UnavailableEmbedder:
    async def embed(self, text: str) -> list[float]:
        raise EmbedderUnavailable("provider down")


async def test_a_missing_question_is_not_swallowed_by_the_degrade_path(
    chunk_file, question_file
) -> None:
    """Red when: ``MissingRecordedVector`` becomes an ``EmbedderUnavailable``."""
    shipped = _shipped()
    embedder = _recorded_embedder(chunk_file, question_file)
    retriever = HybridRetriever(session=None)  # type: ignore[arg-type]
    # Partner: the same path really does swallow a provider outage, so the raise
    # below is a property of the error type and not of a path that swallows nothing.
    _, degrade = await retriever._safe_vector_retrieve(
        _ArmThatEmbeds(_UnavailableEmbedder()),  # type: ignore[arg-type]
        "question",
        product_area="codex",
        limit=5,
    )
    assert degrade is VectorDegrade.unavailable
    with pytest.raises(shipped.MissingRecordedVector):
        await retriever._safe_vector_retrieve(
            _ArmThatEmbeds(embedder),  # type: ignore[arg-type]
            "A question nobody recorded",
            product_area="codex",
            limit=5,
        )


# --- Stamps and seam refusals ------------------------------------------------------


def test_each_identity_comes_from_its_own_header(tmp_path, question_file) -> None:
    """Red when: the index or query stamp stops being read from its own file's header."""
    chunks = write_chunk_file(
        tmp_path / "c.jsonl",
        {"codex#CLI flags": ("c" * 64, [1.0, 0.0, 0.0, 0.0])},
        header={**CHUNK_HEADER, "model": "the-index-model"},
    )
    embedder = _recorded_embedder(chunks, question_file)
    assert embedder.index_identity == EmbedderIdentity("gemini", "the-index-model", 4)
    assert embedder.query_identity == EmbedderIdentity("gemini", "gemini-embedding-001", 4)


@pytest.mark.parametrize("field", ["provider", "model", "dim"])
def test_a_none_stamp_in_a_header_is_refused(field: str) -> None:
    """Red when: a null header stamp is accepted; it would leave the gate open."""
    shipped = _shipped()
    with pytest.raises(shipped.ShippedEvalError, match=field):
        shipped.identity_from_header({**QUESTION_HEADER, field: None})


# Nothing listens here, so a seam that connects before refusing raises a driver error
# and fails the ``pytest.raises`` below: refusals must come first.
_UNREACHABLE_POSTGRES = "postgresql+psycopg://nobody:nothing@127.0.0.1:1/none"


async def _enter_seam(settings: Any, embedder: Any, identity: Any) -> None:
    async with _shipped().shipped_corpus_session(settings, embedder, identity):
        pytest.fail("the seam yielded where it had to refuse")


@pytest.mark.parametrize(
    "identity",
    [
        EmbedderIdentity(None, None, None),
        EmbedderIdentity("gemini", None, 4),
        EmbedderIdentity("gemini", "gemini-embedding-001", None),
    ],
)
async def test_the_seam_refuses_a_none_query_stamp(chunk_file, question_file, identity) -> None:
    """Red when: the seam accepts a None stamp, which ``is_index_embedder_mismatch`` allows."""
    settings = SimpleNamespace(environment="test", database_url=_UNREACHABLE_POSTGRES)
    embedder = _recorded_embedder(chunk_file, question_file)
    with pytest.raises(_shipped().ShippedEvalError, match="stamp"):
        await _enter_seam(settings, embedder, identity)


async def test_the_seam_refuses_production(chunk_file, question_file) -> None:
    """Red when: the seam's production refusal is removed."""
    settings = SimpleNamespace(environment="production", database_url=_UNREACHABLE_POSTGRES)
    embedder = _recorded_embedder(chunk_file, question_file)
    with pytest.raises(_shipped().ShippedEvalError, match="production"):
        await _enter_seam(settings, embedder, embedder.query_identity)


async def test_the_seam_refuses_a_database_that_is_not_postgres(chunk_file, question_file) -> None:
    """Red when: the seam's non-Postgres refusal is removed."""
    settings = SimpleNamespace(environment="test", database_url="sqlite+aiosqlite:///:memory:")
    embedder = _recorded_embedder(chunk_file, question_file)
    with pytest.raises(_shipped().ShippedEvalError, match="Postgres"):
        await _enter_seam(settings, embedder, embedder.query_identity)


# --- Labels ------------------------------------------------------------------------


def _golden_row(case_id: str = "case", kind: str = "literal", **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": case_id,
        "area": "codex",
        "kind": kind,
        "question": "What does the --model flag do in Codex?",
        "expected_source": "codex",
        "expected_gist": "It selects the model.",
        "expect_no_answer": False,
    }
    row.update(extra)
    return row


def _refusal_row(**extra: Any) -> dict[str, Any]:
    return _golden_row(
        "refused",
        "refusal",
        area="out_of_corpus",
        expected_source=None,
        expect_no_answer=True,
        **extra,
    )


def _write_rows(path: pathlib.Path, rows: Iterable[Mapping[str, Any]]) -> pathlib.Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


HEADINGS = {"codex#CLI flags": "c" * 64, "codex#CLI sandbox": "d" * 64}


def test_the_real_golden_set_is_fully_labelled_against_the_44_headings() -> None:
    """Red when: an answerable golden row loses its label, or a heading is renamed."""
    shipped = _shipped()
    headings = shipped.shipped_heading_keys()
    assert len(headings) == SHIPPED_PASSAGES
    cases = shipped.load_golden_labels(GOLDEN, headings)
    answerable = [case for case in cases if case.kind != "refusal"]
    refusals = [case for case in cases if case.kind == "refusal"]
    assert len(answerable) == GOLDEN_ANSWERABLE
    assert len(refusals) == GOLDEN_REFUSALS
    labelled_keys = [key for case in answerable for key in case.shipped_gold]
    # Partner to "every key exists": there are keys, and they reach several sources.
    assert len(labelled_keys) > GOLDEN_ANSWERABLE
    assert len({key.split("#")[0] for key in labelled_keys}) == 6
    assert set(labelled_keys) <= set(headings)
    assert all(case.shipped_gold == () for case in refusals)


def test_a_golden_row_missing_the_key_fails_and_names_the_row(tmp_path) -> None:
    """Red when: an unreviewed answerable row can pass as if it had been labelled."""
    shipped = _shipped()
    path = _write_rows(tmp_path / "g.jsonl", [_golden_row("unreviewed")])
    with pytest.raises(shipped.ShippedLabelError, match="unreviewed"):
        shipped.load_golden_labels(path, HEADINGS)


def test_an_empty_label_is_accepted_as_reviewed(tmp_path) -> None:
    """Red when: ``[]`` stops meaning reviewed, or a key check rejects it."""
    shipped = _shipped()
    path = _write_rows(tmp_path / "g.jsonl", [_golden_row("reviewed", shipped_gold=[])])
    assert shipped.load_golden_labels(path, HEADINGS)[0].shipped_gold == ()


def test_a_refusal_may_not_carry_a_label(tmp_path) -> None:
    """Red when: a refusal row can carry ``shipped_gold``."""
    shipped = _shipped()
    path = _write_rows(tmp_path / "g.jsonl", [_refusal_row(shipped_gold=[])])
    with pytest.raises(shipped.ShippedLabelError, match="refused"):
        shipped.load_golden_labels(path, HEADINGS)


def test_a_label_that_is_not_a_heading_fails_and_names_the_key(tmp_path) -> None:
    """Red when: a gold key is no longer checked against the real headings."""
    shipped = _shipped()
    path = _write_rows(tmp_path / "g.jsonl", [_golden_row(shipped_gold=["codex#No such heading"])])
    with pytest.raises(shipped.ShippedLabelError, match="codex#No such heading"):
        shipped.load_golden_labels(path, HEADINGS)


def test_the_real_questions_cover_every_kind_and_every_gold_key_exists() -> None:
    """Red when: a coverage kind is emptied, or a question's gold key stops existing."""
    shipped = _shipped()
    headings = shipped.shipped_heading_keys()
    cases = shipped.load_shipped_questions(QUESTIONS, headings)
    assert 60 <= len(cases) <= 75
    per_kind = {kind: 0 for kind in COVERAGE_KINDS}
    for case in cases:
        per_kind[case.coverage_kind] += 1
    assert set(per_kind) == COVERAGE_KINDS
    assert min(per_kind.values()) >= 2, per_kind
    assert all(case.origin == "coverage" for case in cases)
    assert sum(len(case.shipped_gold) for case in cases) > len(cases) / 2
    assert {key for case in cases for key in case.shipped_gold} <= set(headings)


def test_a_negative_question_has_no_gold_and_a_positive_one_has_some() -> None:
    """Red when: an expected-empty question gains gold, or a positive one loses it."""
    shipped = _shipped()
    cases = shipped.load_shipped_questions(QUESTIONS, shipped.shipped_heading_keys())
    negatives = [case for case in cases if case.expected_empty]
    positives = [case for case in cases if not case.expected_empty]
    assert len(negatives) >= 5
    assert len(positives) >= 50
    assert all(case.shipped_gold == () for case in negatives)
    assert all(case.shipped_gold for case in positives)


def test_a_question_marked_empty_may_not_carry_gold(tmp_path) -> None:
    """Red when: the loader stops rejecting an expected-empty question with gold."""
    shipped = _shipped()
    row = {
        "id": "negative",
        "question": "What does --modell do in Codex?",
        "coverage_kind": "exact",
        "shipped_gold": ["codex#CLI flags"],
        "expected_empty": True,
        "expected_empty_scope": "exact_arm",
    }
    path = _write_rows(tmp_path / "q.jsonl", [row])
    with pytest.raises(shipped.ShippedLabelError, match="negative"):
        shipped.load_shipped_questions(path, HEADINGS)


def test_the_questions_carry_the_planned_370_tokens_and_non_english_phrasing() -> None:
    """Red when: a token #370 is measured on, or a non-English question, is removed."""
    shipped = _shipped()
    cases = shipped.load_shipped_questions(QUESTIONS, shipped.shipped_heading_keys())
    planned = " ".join(case.question for case in cases if case.planned_for == "370")
    assert [token for token in PLANNED_370_TOKENS if token not in planned] == []
    facets = {case.facet for case in cases if case.coverage_kind == "semantic"}
    assert {"zero_overlap", "synonym", "misspelling", "long_rambling", "hindi"} <= facets
    assert len(facets & {"spanish", "romanized_hindi"}) >= 1
    # The facet tag alone proves nothing: the Hindi row must really be in Devanagari.
    hindi = [case.question for case in cases if case.facet == "hindi"]
    assert hindi
    assert all(any("\u0900" <= ch <= "\u097f" for ch in question) for question in hindi)


# --- Report ------------------------------------------------------------------------


def _outcome(case_id: str, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "case_id": case_id,
        "origin": "golden",
        "kind": "literal",
        "coverage_kind": None,
        "gold": ("codex#CLI flags",),
        "retrieved": ("codex#CLI flags", "codex#CLI sandbox"),
        "route": "scoped",
        "winning_arm": "hybrid",
        "routed_domain": "codex",
        "degrade": VectorDegrade.none,
        "follow_up": False,
    }
    fields.update(overrides)
    return _shipped().ShippedOutcome(**fields)


def _decision_outcomes() -> list[Any]:
    """Seven questions: two in the decision pool, one of each exclusion."""
    return [
        _outcome("in_pool_hit"),
        _outcome("in_pool_second", retrieved=("codex#CLI sandbox", "codex#CLI flags")),
        _outcome("a_follow_up", kind="followup", follow_up=True),
        _outcome("a_multihop", kind="multihop", gold=("codex#CLI flags", "codex#CLI sandbox")),
        _outcome("no_gold", gold=()),
        _outcome("a_refusal", kind="refusal", gold=(), retrieved=("codex#CLI sandbox",)),
        _outcome(
            "a_coverage",
            origin="coverage",
            kind=None,
            coverage_kind="exact",
        ),
    ]


def test_decision_a_is_the_golden_single_relevant_non_follow_up_pool() -> None:
    """Red when: any excluded class leaks into decision A's number."""
    decision = _shipped().build_report(_decision_outcomes())["decision_a"]
    assert decision["pool"] == 2
    assert decision["precision_at_1"] == pytest.approx(0.5)
    assert decision["recall_at_1"] == pytest.approx(0.5)
    assert decision["mrr"] == pytest.approx(0.75)


def test_decision_a_exclusions_add_up_to_the_total() -> None:
    """Red when: an excluded count is dropped, doubled or misfiled."""
    decision = _shipped().build_report(_decision_outcomes())["decision_a"]
    assert decision["excluded"] == {
        "follow_up": 1,
        "multi_relevant": 1,
        "no_single_gold": 1,
        "refusal": 1,
        "coverage_set": 1,
    }
    assert decision["total"] == 7
    assert decision["pool"] + sum(decision["excluded"].values()) == decision["total"]


def test_the_printed_summary_shows_the_pool_and_every_exclusion() -> None:
    """Red when: the summary hides the pool size or an exclusion count."""
    shipped = _shipped()
    summary = shipped.render_summary(shipped.build_report(_decision_outcomes()))
    assert "pool" in summary.lower()
    for label in ("follow_up", "multi_relevant", "no_single_gold", "refusal", "coverage_set"):
        assert label in summary


def test_follow_ups_are_excluded_from_decision_a_but_labelled_per_question() -> None:
    """Red when: a follow-up counts toward decision A, or loses its label in the report."""
    shipped = _shipped()
    with_follow_up = shipped.build_report(
        [_outcome("hit"), _outcome("f", follow_up=True, retrieved=())]
    )
    # Partner: the same row without the flag does lower the number.
    without_flag = shipped.build_report([_outcome("hit"), _outcome("f", retrieved=())])
    assert with_follow_up["decision_a"]["precision_at_1"] == pytest.approx(1.0)
    assert without_flag["decision_a"]["precision_at_1"] == pytest.approx(0.5)
    rows = {row["id"]: row for row in with_follow_up["questions"]}
    assert rows["f"]["follow_up"] is True
    assert rows["hit"]["follow_up"] is False


def test_metrics_are_aggregated_by_kind_and_by_coverage_kind() -> None:
    """Red when: recall@k, MRR or precision@1 is computed wrongly or a grouping is lost."""
    shipped = _shipped()
    report = shipped.build_report(
        [
            _outcome("rank_one"),
            _outcome(
                "rank_four",
                retrieved=("a#1", "a#2", "a#3", "codex#CLI flags"),
            ),
            _outcome(
                "coverage_multi",
                origin="coverage",
                kind=None,
                coverage_kind="multi_product",
                gold=("codex#CLI flags", "codex#CLI sandbox"),
                retrieved=("codex#CLI flags", "a#2", "a#3", "codex#CLI sandbox"),
            ),
        ]
    )
    literal = report["by_kind"]["literal"]
    assert literal["n"] == 2
    assert literal["recall_at_1"] == pytest.approx(0.5)
    assert literal["recall_at_3"] == pytest.approx(0.5)
    assert literal["recall_at_6"] == pytest.approx(1.0)
    assert literal["mrr"] == pytest.approx((1.0 + 0.25) / 2)
    assert literal["precision_at_1"] == pytest.approx(0.5)
    multi = report["by_coverage_kind"]["multi_product"]
    assert multi["n"] == 1
    assert multi["recall_at_1"] == pytest.approx(0.5)
    assert multi["recall_at_6"] == pytest.approx(1.0)


def test_route_winning_arm_and_rank_are_reported_per_question() -> None:
    """Red when: a question's route, winning arm or rank is dropped from the report."""
    shipped = _shipped()
    report = shipped.build_report(
        [
            _outcome("scoped_one"),
            _outcome(
                "global_one",
                route="global_vector_only",
                winning_arm="vector",
                routed_domain="unsupported",
                retrieved=("a#1", "codex#CLI flags"),
            ),
            _outcome("no_hits", retrieved=(), winning_arm=None),
        ]
    )
    rows = {row["id"]: row for row in report["questions"]}
    assert (rows["scoped_one"]["route"], rows["scoped_one"]["winning_arm"]) == ("scoped", "hybrid")
    assert rows["global_one"]["route"] == "global_vector_only"
    assert rows["global_one"]["winning_arm"] == "vector"
    assert rows["global_one"]["routed_domain"] == "unsupported"
    assert rows["global_one"]["rank"] == 2
    assert rows["no_hits"]["rank"] is None


@pytest.mark.parametrize(
    "overrides",
    [{"route": None}, {"route": ""}, {"winning_arm": None}],
    ids=["no_route", "blank_route", "hits_without_winning_arm"],
)
def test_a_question_without_a_route_or_arm_is_refused(overrides: dict[str, Any]) -> None:
    """Red when: a report can be built without route and arm, merging #370 and #553."""
    shipped = _shipped()
    with pytest.raises(shipped.ShippedEvalError, match="unlabelled"):
        shipped.build_report([_outcome("unlabelled", **overrides)])


@pytest.mark.parametrize(
    "degrade",
    [VectorDegrade.mismatch, VectorDegrade.ambiguous_index, VectorDegrade.unavailable],
)
def test_any_degrade_is_fatal_to_the_report(degrade: VectorDegrade) -> None:
    """Red when: a degraded question can lower a number instead of stopping the report."""
    shipped = _shipped()
    with pytest.raises(shipped.ShippedEvalError, match="degraded_one"):
        shipped.build_report([_outcome("clean"), _outcome("degraded_one", degrade=degrade)])
    # Partner: the clean row alone builds a report.
    assert shipped.build_report([_outcome("clean")])["decision_a"]["pool"] == 1


def test_a_report_over_no_questions_is_refused() -> None:
    """Red when: an empty run can print a report that looks like a measurement."""
    shipped = _shipped()
    with pytest.raises(shipped.ShippedEvalError):
        shipped.build_report([])


def test_scoped_refusal_hits_are_informational_and_counted() -> None:
    """Red when: refusal retrieval stops being reported, or starts failing the report."""
    report = _shipped().build_report(_decision_outcomes())
    assert report["refusals"] == {"total": 1, "retrieved_something": 1}


def test_the_report_is_written_to_json_and_appended_to_the_summary(tmp_path) -> None:
    """Red when: the JSON is not written, or the job summary is overwritten."""
    shipped = _shipped()
    report = shipped.build_report(_decision_outcomes())
    json_path = tmp_path / "out" / "shipped.json"
    summary_path = tmp_path / "summary.md"
    summary_path.write_text("earlier step\n", encoding="utf-8")
    shipped.write_report(report, json_path, summary_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["decision_a"]["pool"] == 2
    written = summary_path.read_text(encoding="utf-8")
    assert written.startswith("earlier step\n")
    assert shipped.render_summary(report) in written


def test_the_postgres_vacuity_guard_watches_the_new_file() -> None:
    """Red when: the shipped Postgres suite can skip in CI with no guard noticing."""
    guard = (BACKEND / "tests" / "test_postgres_suite_is_not_vacuous.py").read_text(
        encoding="utf-8"
    )
    assert "test_eval_shipped_pg" in guard
