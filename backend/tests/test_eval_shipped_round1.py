"""#601 review round 1: the shipped-corpus report's honesty checks (free default job)."""

from __future__ import annotations

import dataclasses
import json
import pathlib
from typing import Any

import pytest

from app.core.config import get_settings
from app.embeddings.factory import EmbedderIdentity
from app.retrieval.types import VectorDegrade
from tests.eval import record_vectors, shipped
from tests.eval.paths import EVAL_DIR, RECORDED_CHUNKS_PATH, RECORDED_QUESTIONS_PATH
from tests.eval.recorded import ShippedEvalError, encode_vector, load_chunk_vectors
from tests.eval.recorded import load_question_vectors as load_questions

README = EVAL_DIR / "README.md"
RE_RECORD_VARIABLES = (
    "CITEVYN_EMBEDDING_PROVIDER",
    "CITEVYN_EMBEDDING_MODEL",
    "CITEVYN_GEMINI_API_KEY",
    "CITEVYN_DATABASE_URL",
    "--export-chunks-from",
)


def _outcome(case_id: str, **overrides: Any) -> shipped.ShippedOutcome:
    fields: dict[str, Any] = {
        "case_id": case_id,
        "origin": "golden",
        "kind": "literal",
        "coverage_kind": None,
        "gold": ("codex#CLI flags",),
        "retrieved": ("codex#CLI flags",),
        "route": "scoped",
        "winning_arm": "vector",
        "routed_domain": "codex",
        "degrade": VectorDegrade.none,
        "follow_up": False,
    }
    fields.update(overrides)
    return shipped.ShippedOutcome(**fields)


# --- 3. The recording is production's vector space ---------------------------------


def test_the_committed_recording_is_in_productions_vector_space() -> None:
    """Red when: the committed headers stop matching production's embedder."""
    production = shipped.production_embedder_identity()
    # Partner: production's identity is read from its config, and is a real provider.
    assert production.provider not in (None, "stub")
    assert production.dim == get_settings().embedding_dim
    shipped.check_production_identity(
        load_chunk_vectors(RECORDED_CHUNKS_PATH).header,
        load_questions(RECORDED_QUESTIONS_PATH).header,
    )


def test_a_lowercase_fly_setting_is_read_as_settings_reads_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red when: a lowercase ``fly.toml`` key, which Settings honours, slips past the pin."""
    (tmp_path / "fly.toml").write_text(
        '[env]\n  CITEVYN_EMBEDDING_PROVIDER = "gemini"\n'
        '  citevyn_embedding_model = "other-model"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(shipped, "REPO_ROOT", tmp_path)
    identity = shipped.production_embedder_identity()
    assert (identity.provider, identity.model) == ("gemini", "other-model")


def test_a_recording_from_another_model_is_refused() -> None:
    """Red when: a recording made with a model production does not use can pass."""
    production = shipped.production_embedder_identity()
    header = {"provider": production.provider, "model": "other-model", "dim": production.dim}
    with pytest.raises(ShippedEvalError, match="other-model"):
        shipped.check_production_identity(header, header)


def test_the_recorder_refuses_an_embedder_production_does_not_use(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: the recorder can write vectors from a model production does not use."""
    other = get_settings().model_copy(
        update={"embedding_provider": "gemini", "embedding_model": "other-model"}
    )
    monkeypatch.setattr(record_vectors, "get_settings", lambda: other)
    monkeypatch.setattr(record_vectors, "RECORDED_QUESTIONS_PATH", tmp_path / "q.jsonl")
    monkeypatch.setattr(record_vectors, "RECORDED_CHUNKS_PATH", tmp_path / "c.jsonl")
    assert record_vectors.main([]) == 1
    assert "other-model" in capsys.readouterr().err
    assert not (tmp_path / "q.jsonl").exists()


# --- 4. Pool coverage --------------------------------------------------------------


def test_the_report_discloses_how_many_passages_the_pool_covers() -> None:
    """Red when: a pool dominated by one passage can pass as a broad measurement."""
    report = shipped.build_report(
        [
            _outcome("a"),
            _outcome("b"),
            _outcome("c"),
            _outcome("d", gold=("codex#CLI sandbox",), retrieved=("codex#CLI sandbox",)),
        ]
    )
    decision = report["decision_a"]
    assert decision["distinct_passages"] == 2
    assert decision["largest_passage_share"] == pytest.approx(0.75)
    summary = shipped.render_summary(report)
    assert "2 distinct passages" in summary
    assert "0.750" in summary


# --- 1. Arm attribution label ------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "label"),
    [((), "none"), (("vector",), "vector"), (("keyword",), "keyword"), (("exact",), "exact")]
    + [(("vector", "keyword"), "both"), (("keyword", "exact"), "both")],
)
def test_the_arm_is_named_by_which_removal_changes_rank_one(
    changes: tuple[str, ...], label: str
) -> None:
    """Red when: the arm label stops following the removal evidence."""
    assert shipped.arm_label(changes) == label


def test_the_summary_counts_questions_a_non_vector_arm_decided() -> None:
    """Red when: the summary hides how often keyword or exact search changed rank 1."""
    report = shipped.build_report(
        [
            _outcome("v", rank1_changed_by=("vector",)),
            _outcome("k", rank1_changed_by=("keyword",)),
            _outcome("n", rank1_changed_by=()),
        ]
    )
    assert report["arm_attribution"]["non_vector_changed_rank_1"] == 1
    assert report["arm_attribution"]["total"] == 3
    rows = {row["id"]: row for row in report["questions"]}
    assert rows["k"]["arm"] == "keyword"
    assert rows["n"]["arm"] == "none"
    summary = shipped.render_summary(report)
    assert "a non-vector arm changed rank 1: 1 of 3" in summary


# --- Clarify-routed questions ------------------------------------------------------


def test_a_question_production_does_not_retrieve_for_is_labelled() -> None:
    """Red when: a question production short-circuits is reported as if it were served."""
    settings = get_settings()
    case = shipped.ShippedCase(
        id="short",
        question="?",
        origin="coverage",
        kind=None,
        coverage_kind="keyword",
        shipped_gold=(),
        expected_empty=True,
    )
    routed = shipped.route_case(case, settings)
    no_global = shipped.route_case(
        case, settings.model_copy(update={"answer_when_grounded": False})
    )
    # Partner: an ordinary question is served.
    served = shipped.route_case(
        dataclasses.replace(case, question="How does the Codex sandbox work?"), settings
    )
    assert served.production_skip is None
    assert routed.production_skip in (None, "clarify")
    assert no_global.production_skip in ("clarify", "refused")
    report = shipped.build_report(
        [_outcome("skipped", production_skip="clarify"), _outcome("served")]
    )
    rows = {row["id"]: row for row in report["questions"]}
    assert rows["skipped"]["skipped_in_production"] == "clarify"
    assert rows["served"]["skipped_in_production"] is None


# --- 9. Follow-up exclusion --------------------------------------------------------


def test_forcing_follow_up_off_changes_the_pool() -> None:
    """Red when: follow-ups leave decision A by kind alone, so the flag decides nothing."""
    cases = shipped.all_shipped_cases()
    outcomes = [
        _outcome(
            case.id,
            origin=case.origin,
            kind=case.kind,
            coverage_kind=case.coverage_kind,
            gold=case.shipped_gold,
            retrieved=case.shipped_gold,
            follow_up=case.follow_up,
        )
        for case in cases
    ]
    single_gold_follow_ups = [
        c for c in cases if c.origin == "golden" and c.follow_up and len(c.shipped_gold) == 1
    ]
    assert single_gold_follow_ups
    pool = shipped.build_report(outcomes)["decision_a"]["pool"]
    forced = [dataclasses.replace(o, follow_up=False) for o in outcomes]
    assert shipped.build_report(forced)["decision_a"]["pool"] == pool + len(single_gold_follow_ups)


# --- 6. The paid-call guard --------------------------------------------------------


class _CountingEmbedder:
    def __init__(self) -> None:
        self.documents: list[str] = []
        self.queries: list[str] = []

    @property
    def dim(self) -> int:
        return 1536

    async def embed(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0] * 1536

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.documents.extend(texts)
        return [[1.0] * 1536 for _ in texts]


def test_without_the_flag_a_changed_chunk_is_never_embedded(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red when: a run without ``--embed-changed-chunks`` can pay to embed chunks."""
    production = shipped.production_embedder_identity()
    real = get_settings().model_copy(
        update={
            "embedding_provider": production.provider,
            "embedding_model": production.model,
            "embedding_dim": production.dim,
        }
    )
    embedder = _CountingEmbedder()
    chunks = tmp_path / "c.jsonl"
    # Every chunk but one is recorded; the last has changed text.
    current = shipped.shipped_heading_keys()
    changed = sorted(current)[-1]
    header = record_vectors.chunk_header(production, "sha256:" + "0" * 64)
    lines = [json.dumps(header)] + [
        json.dumps(
            {
                "chunk": key,
                "content_checksum": "f" * 64 if key == changed else checksum,
                "vector": encode_vector([1.0] * 1536),
            }
        )
        for key, checksum in sorted(current.items())
    ]
    chunks.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(record_vectors, "get_settings", lambda: real)
    monkeypatch.setattr(record_vectors, "RECORDED_CHUNKS_PATH", chunks)
    monkeypatch.setattr(record_vectors, "RECORDED_QUESTIONS_PATH", tmp_path / "q.jsonl")
    monkeypatch.setattr(record_vectors, "build_embedder", lambda _settings: embedder)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    monkeypatch.setattr(record_vectors, "metered_embedder", lambda inner, _settings: inner)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert record_vectors.main([]) == 1
    assert changed in capsys.readouterr().err
    assert embedder.documents == []
    assert embedder.queries == []


# --- 8. Re-record instructions -----------------------------------------------------


def test_the_re_record_instructions_name_every_variable_and_the_approval() -> None:
    """Red when: a failure tells someone to re-record without what they need, or the cost."""
    readme = README.read_text(encoding="utf-8")
    for text in (shipped.RECORD_COMMAND, readme):
        assert [name for name in RE_RECORD_VARIABLES if name not in text] == []
        assert "paid provider call" in text
        assert "owner's approval" in text


# --- Advisory: export URL, duplicates, task type, schema ---------------------------


@pytest.mark.parametrize("field", ["host", "hostaddr", "service"])
async def test_the_export_refuses_a_host_hidden_in_the_query(field: str) -> None:
    """Red when: ``?host=`` can point a 'local' URL at a hosted database."""
    with pytest.raises(ShippedEvalError, match="local"):
        await record_vectors.export_chunk_vectors(
            f"postgresql+psycopg://reader:hidden@localhost/app?{field}=db.example.com",
            identity=EmbedderIdentity("gemini", "gemini-embedding-001", 1536),
        )


HEADER = {"schema": 1, "provider": "gemini", "model": "m", "dim": 1, "task_type": ""}


def _write(path: pathlib.Path, header: dict[str, Any], rows: list[dict[str, Any]]) -> pathlib.Path:
    path.write_text("\n".join(json.dumps(r) for r in [header, *rows]) + "\n", encoding="utf-8")
    return path


def test_a_duplicate_chunk_row_is_refused(tmp_path: pathlib.Path) -> None:
    """Red when: a second vector for one chunk silently replaces the first."""
    row = {"chunk": "s#a", "content_checksum": "c", "vector": encode_vector([1.0])}
    header = {**HEADER, "task_type": "RETRIEVAL_DOCUMENT", "corpus_hash": "x"}
    with pytest.raises(ShippedEvalError, match="duplicate"):
        load_chunk_vectors(_write(tmp_path / "c.jsonl", header, [row, row]))


def test_a_duplicate_case_id_is_refused(tmp_path: pathlib.Path) -> None:
    """Red when: two questions with one id can overwrite each other in the report."""
    row = {"id": "same", "question": "q", "coverage_kind": "exact", "shipped_gold": ["s#a"]}
    path = tmp_path / "q.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(shipped.ShippedLabelError, match="same"):
        shipped.load_shipped_questions(path, {"s#a": "c"})


def test_a_query_file_with_the_document_task_type_is_refused(tmp_path: pathlib.Path) -> None:
    """Red when: document vectors can be loaded as question vectors."""
    header = {**HEADER, "task_type": "RETRIEVAL_DOCUMENT"}
    with pytest.raises(ShippedEvalError, match="task_type"):
        load_questions(_write(tmp_path / "q.jsonl", header, []))


def test_an_unknown_schema_is_refused(tmp_path: pathlib.Path) -> None:
    """Red when: a file in a future layout is read as if it were this one."""
    header = {**HEADER, "schema": 2, "task_type": "RETRIEVAL_QUERY"}
    with pytest.raises(ShippedEvalError, match="schema"):
        load_questions(_write(tmp_path / "q.jsonl", header, []))
