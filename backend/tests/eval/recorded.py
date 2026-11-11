"""Recorded embedding vectors for the shipped-corpus eval (#601).

Pure loading and comparison: no database, no provider. Two committed files under
``tests/eval/recorded/`` hold one vector per shipped chunk and one per exact question
text. Each file's first line is a header stating the vector space it was recorded in.

:class:`RecordedEmbedder` serves those vectors through the real :class:`Embedder` seam, so
retrieval runs unchanged. A text nobody recorded raises :class:`MissingRecordedVector`,
deliberately not an :class:`~app.embeddings.errors.EmbedderUnavailable`: the retriever
swallows that type and carries on with keyword search, which would turn a missing fixture
into a quietly lower number.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import json
import pathlib
import struct
from collections.abc import Mapping
from typing import Any, cast

from app.embeddings.factory import EmbedderIdentity

SCHEMA_VERSION = 1
TASK_TYPE_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_TYPE_QUERY = "RETRIEVAL_QUERY"
_HEADER_FIELDS = ("schema", "provider", "model", "dim", "task_type")

RECORD_COMMAND = (
    "python -m tests.eval.record_vectors --export-chunks-from <local Postgres URL> "
    "--embed-changed-chunks, run from backend/ with CITEVYN_EMBEDDING_PROVIDER, "
    "CITEVYN_EMBEDDING_MODEL and CITEVYN_GEMINI_API_KEY set for production's embedder and "
    "CITEVYN_DATABASE_URL naming a migrated local database. A re-record is a paid provider "
    "call and needs the owner's approval; see tests/eval/README.md"
)


class ShippedEvalError(RuntimeError):
    """The shipped-corpus eval cannot produce an honest number, so it stops."""


class MissingRecordedVector(ShippedEvalError):
    """A text was embedded that the recording does not hold."""


class RecordingMismatch(ShippedEvalError):
    """The recorded chunk vectors do not describe the current shipped corpus."""

    def __init__(self, *, changed: list[str], missing: list[str], stale: list[str]) -> None:
        self.changed = changed
        self.missing = missing
        self.stale = stale
        parts = [
            f"{label}: {', '.join(keys)}"
            for label, keys in (("changed", changed), ("missing", missing), ("stale", stale))
            if keys
        ]
        super().__init__(
            f"the chunk recording does not match the shipped corpus ({'; '.join(parts)}). "
            f"Re-record with: {RECORD_COMMAND}"
        )


@dataclasses.dataclass(frozen=True)
class ChunkVector:
    content_checksum: str
    vector: list[float]


@dataclasses.dataclass(frozen=True)
class Recording[T]:
    header: Mapping[str, Any]
    rows: Mapping[str, T]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_vector(vector: list[float]) -> str:
    """Base64 of little-endian float32, the committed form of a vector."""
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


def _decode_vector(encoded: object, *, dim: int, row: str) -> list[float]:
    if not isinstance(encoded, str):
        raise ShippedEvalError(f"row {row!r}: the vector is not a base64 string")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ShippedEvalError(f"row {row!r}: the vector is not valid base64") from exc
    if len(raw) != dim * 4:
        raise ShippedEvalError(
            f"row {row!r}: the vector holds {len(raw) // 4} values; the header says dim {dim}"
        )
    return list(struct.unpack(f"<{dim}f", raw))


def _require_stamp(header: Mapping[str, Any], field: str) -> Any:
    value = header.get(field)
    if value is None:
        raise ShippedEvalError(f"the recording header has no stamp for {field!r}")
    return value


def identity_from_header(header: Mapping[str, Any]) -> EmbedderIdentity:
    """The vector-space stamp a header claims. A ``None`` field would open the gate."""
    return EmbedderIdentity(
        provider=_require_stamp(header, "provider"),
        model=_require_stamp(header, "model"),
        dim=_require_stamp(header, "dim"),
    )


def _read_recording(
    path: pathlib.Path, *, task_type: str, extra_fields: tuple[str, ...] = ()
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The validated header and the raw rows of one recording file."""
    if not path.is_file():
        raise ShippedEvalError(f"no recording at {path.name}. Record with: {RECORD_COMMAND}")
    records: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ShippedEvalError(f"{path.name}:{lineno}: not valid JSON") from exc
        if not isinstance(record, dict):
            raise ShippedEvalError(f"{path.name}:{lineno}: each line must be a JSON object")
        records.append(cast("dict[str, Any]", record))
    if not records:
        raise ShippedEvalError(f"{path.name} is empty")
    header, *rows = records
    for field in (*_HEADER_FIELDS, *extra_fields):
        if header.get(field) is None:
            raise ShippedEvalError(f"{path.name}: the header has no {field!r}")
    if header["schema"] != SCHEMA_VERSION:
        raise ShippedEvalError(f"{path.name}: unsupported schema {header['schema']!r}")
    if header["task_type"] != task_type:
        raise ShippedEvalError(
            f"{path.name}: task_type {header['task_type']!r}, expected {task_type!r}"
        )
    if not isinstance(header["dim"], int) or isinstance(header["dim"], bool) or header["dim"] < 1:
        raise ShippedEvalError(f"{path.name}: dim must be a positive integer")
    return header, rows


def _field(row: Mapping[str, Any], field: str, *, source: str) -> Any:
    if field not in row:
        raise ShippedEvalError(f"{source}: a row has no {field!r}")
    return row[field]


def load_chunk_vectors(path: pathlib.Path) -> Recording[ChunkVector]:
    header, raw_rows = _read_recording(
        path, task_type=TASK_TYPE_DOCUMENT, extra_fields=("corpus_hash",)
    )
    rows: dict[str, ChunkVector] = {}
    for raw in raw_rows:
        key = str(_field(raw, "chunk", source=path.name))
        if key in rows:
            raise ShippedEvalError(f"{path.name}: duplicate chunk {key!r}")
        rows[key] = ChunkVector(
            content_checksum=str(_field(raw, "content_checksum", source=key)),
            vector=_decode_vector(
                _field(raw, "vector", source=key), dim=int(header["dim"]), row=key
            ),
        )
    return Recording(header=header, rows=rows)


def load_question_vectors(path: pathlib.Path) -> Recording[list[float]]:
    header, raw_rows = _read_recording(path, task_type=TASK_TYPE_QUERY)
    rows: dict[str, list[float]] = {}
    for raw in raw_rows:
        key = str(_field(raw, "text_sha256", source=path.name))
        if key in rows:
            raise ShippedEvalError(f"{path.name}: duplicate question {key!r}")
        rows[key] = _decode_vector(
            _field(raw, "vector", source=key), dim=int(header["dim"]), row=key
        )
    return Recording(header=header, rows=rows)


def check_chunk_vectors(recorded: Recording[ChunkVector], current: Mapping[str, str]) -> int:
    """How many chunks matched. Any difference raises, naming exactly the affected chunks.

    Per chunk on purpose: the header's ``corpus_hash`` is informational, so one edited
    passage names one chunk to re-record rather than failing the whole recording.
    """
    changed = sorted(
        key
        for key, checksum in current.items()
        if key in recorded.rows and recorded.rows[key].content_checksum != checksum
    )
    missing = sorted(key for key in current if key not in recorded.rows)
    stale = sorted(key for key in recorded.rows if key not in current)
    if changed or missing or stale:
        raise RecordingMismatch(changed=changed, missing=missing, stale=stale)
    return len(recorded.rows)


class RecordedEmbedder:
    """An :class:`Embedder` that serves only what was recorded."""

    def __init__(
        self, *, chunks: Recording[ChunkVector], questions: Recording[list[float]]
    ) -> None:
        self.index_identity = identity_from_header(chunks.header)
        self.query_identity = identity_from_header(questions.header)
        dim = self.index_identity.dim
        if dim != self.query_identity.dim or dim is None:
            raise ShippedEvalError(
                f"the chunk recording has dim {dim}; "
                f"the question recording has dim {self.query_identity.dim}"
            )
        self._dim = dim
        self._questions = questions.rows
        self._documents = {row.content_checksum: row.vector for row in chunks.rows.values()}

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, text: str) -> list[float]:
        key = sha256_text(text)
        vector = self._questions.get(key)
        if vector is None:
            raise MissingRecordedVector(
                f"no recorded vector for question text sha256 {key}. Record with: {RECORD_COMMAND}"
            )
        return list(vector)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            key = sha256_text(text)
            vector = self._documents.get(key)
            if vector is None:
                raise MissingRecordedVector(
                    f"no recorded vector for chunk text sha256 {key}. Record with: {RECORD_COMMAND}"
                )
            vectors.append(list(vector))
        return vectors
