"""SQLAlchemy declarative base and shared mixins.

We use SQLAlchemy 2.x style with a single ``Base`` so Alembic sees all
models in one ``Base.metadata``. UUID columns vary by dialect: ``Uuid``
on Postgres (real 128-bit type) and ``CHAR(36)`` on SQLite (string), so
tests can run hermetically without needing a Postgres server.
"""

from __future__ import annotations

import enum
import pickle
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CHAR, DateTime, Float, LargeBinary, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GUID(TypeDecorator):  # type: ignore[type-arg]
    """Platform-independent UUID column.

    Uses the native ``UUID`` type on Postgres and stores as ``CHAR(36)``
    on SQLite. Always accepts and emits :class:`uuid.UUID` instances.
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID

            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any | None:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class StrEnumType(TypeDecorator):  # type: ignore[type-arg]
    """Platform-independent column for :class:`enum.StrEnum` values.

    Migration ``0002_promote_strenum_to_native`` converts every
    ``String(32|64)`` column backed by a ``StrEnum`` to a native
    PostgreSQL ``ENUM`` type. The ORM model still declares the column
    as ``String(32)`` so the hermetic SQLite test suite keeps working,
    which means SQLAlchemy sends a textual parameter to Postgres.
    Postgres does not auto-cast ``VARCHAR`` to a native enum, so we
    intercept the bind: on Postgres we route the value through the
    ``citevyn_<name>`` enum type via a literal cast; on SQLite we
    pass the string value through unchanged.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[enum.StrEnum], length: int = 32) -> None:
        super().__init__(length=length)
        self._enum_cls = enum_cls
        self._length = length
        # The Postgres type name is derived from the migration. Every
        # StrEnum in app.models.enums follows the pattern
        # ``citevyn_<lowercased class name>``.
        self._pg_type_name = f"citevyn_{enum_cls.__name__.lower()}"

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import ENUM

            values = tuple(m.value for m in self._enum_cls)
            return dialect.type_descriptor(
                ENUM(*values, name=self._pg_type_name, create_type=False)
            )
        return dialect.type_descriptor(String(self._length))

    def process_bind_param(self, value: Any, dialect: Any) -> Any | None:
        if value is None:
            return None
        # Always normalise to the underlying string value.
        if isinstance(value, enum.Enum):
            return value.value
        return str(value)

    def process_result_value(self, value: Any, dialect: Any) -> enum.StrEnum | None:
        if value is None:
            return None
        if isinstance(value, self._enum_cls):
            return value
        return self._enum_cls(str(value))


class Base(DeclarativeBase):
    """Project-wide declarative base.

    Subclasses must set ``__tablename__`` explicitly. We avoid the
    snake-case / pluralization helper because the inferred return
    type is not a string literal — SQLAlchemy's ``__tablename__`` is
    declared as a string and pyright's strict mode rejects the
    auto-derivation.
    """


class TimestampMixin:
    """Adds ``created_at`` and ``updated_at`` columns."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()


def uuid_column(*, primary_key: bool = False, **kwargs: Any) -> Mapped[uuid.UUID]:
    """Build a UUID column with sensible defaults."""
    return mapped_column(
        GUID(),
        default=new_uuid,
        primary_key=primary_key,
        nullable=kwargs.pop("nullable", primary_key),
        **kwargs,
    )


def string_pk(**kwargs: Any) -> Mapped[str]:
    """String primary key (used for ``users.user_id`` and ``index_versions``)."""
    return mapped_column(String(128), primary_key=True, **kwargs)


class PickledEmbedding(TypeDecorator):  # type: ignore[type-arg]
    """Portable column type for ``list[float]`` embeddings.

    Stores the list as pickle bytes. On SQLite this is a ``BLOB``;
    on Postgres the same declaration becomes ``bytea``. The
    decorator round-trips Python lists transparently.

    Design notes
    ------------
    * The contract is :class:`list` of :class:`float`. NumPy
      arrays (or any object with a ``.tolist()`` method) are
      the caller's responsibility to convert *before* assigning
      to the column. The embedder pipeline normalises to
      ``list[float]`` at the producer; this decorator is
      intentionally strict so a future regression that lets an
      ndarray through is loud, not silent.
    * ``None`` is preserved as ``None`` (the column is
      nullable). Empty list pickles to a non-None blob, so
      callers that want "no embedding" must set ``None``.
    * Pickle protocol is left at the default (Python's current
      HIGHEST_PROTOCOL) — embeddings are produced in the same
      process that reads them, so cross-version safety is not
      a constraint.
    * The future ``pgvector`` migration (``0004``) will swap
      this column to ``vector(<dim>)`` on Postgres; the
      decorator's pickle contract still works on SQLite for
      tests, and the production path can read the
      ``vector`` column directly via a new decorator.
    * **Security:** never accept pickle bytes from network input.
      The column is written by the worker / orchestrator, never
      by an HTTP route. Adding a route that takes raw bytes
      here would be a remote-code-execution vector. If a future
      client ever needs to push embeddings over the wire,
      serialise as JSON (or ``np.save``/``np.load`` as a flat
      float32 buffer) and decode on the server.
    """

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise TypeError(
                "embedding must be a list[float]; "
                f"got {type(value).__name__}. "
                "Convert numpy arrays with .tolist() at the producer."
            )
        return pickle.dumps(value)

    def process_result_value(self, value: Any, dialect: Any) -> list[float] | None:
        if value is None:
            return None
        # ``value`` arrives as ``bytes`` on both backends. We
        # produced the bytes ourselves in :meth:`process_bind_param`
        # by pickling a plain ``list[float]``, so the unpickled
        # value is already a plain list — no further normalisation
        # is needed and a defensive ``tolist()`` would be dead code.
        return list(pickle.loads(value))


class EmbeddingVector(TypeDecorator):  # type: ignore[type-arg]
    """Portable embedding column: pgvector on Postgres, pickled blob on SQLite.

    This is the ``0004`` successor to :class:`PickledEmbedding`. On Postgres the
    column is a real ``vector(embedding_dim)`` (migration ``0004``), so the
    pgvector cosine-distance operator (``<=>``) runs in the database and the HNSW
    index is used. On SQLite — the hermetic test engine, which has no pgvector —
    the column falls back to a pickled ``list[float]`` blob so the test suite runs
    without a vector database.

    Design notes
    ------------
    * **Dimension source of truth** is ``Settings.embedding_dim``, resolved lazily
      in :meth:`load_dialect_impl` so importing the model does not force a settings
      load. Postgres DDL is owned by the alembic migration, not ORM ``create_all``;
      the ORM ``Vector(dim)`` is used for bind/result processing and query
      compilation.
    * **List contract**, identical to :class:`PickledEmbedding`: the value is a
      ``list[float]``; numpy arrays are the producer's responsibility to convert.
      The strictness keeps a future ndarray regression loud, not silent.
    * **Comparators** (``cosine_distance``/``l2_distance``/``max_inner_product``)
      emit the pgvector operators. They are only ever executed on Postgres — the
      retriever short-circuits before building the query on any other dialect.
    * **Security:** as with :class:`PickledEmbedding`, never accept pickle bytes
      from network input; embeddings are written by the worker/orchestrator only.
    """

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            from app.core.config import get_settings

            return dialect.type_descriptor(Vector(get_settings().embedding_dim))
        return dialect.type_descriptor(LargeBinary())

    def process_bind_param(self, value: Any, dialect: Any) -> Any | None:
        if value is None:
            return None
        if not isinstance(value, list):
            raise TypeError(
                "embedding must be a list[float]; "
                f"got {type(value).__name__}. "
                "Convert numpy arrays with .tolist() at the producer."
            )
        if dialect.name == "postgresql":
            # pgvector's ``Vector`` impl renders the list as a vector literal.
            return cast(list[float], value)
        return pickle.dumps(value)

    def process_result_value(self, value: Any, dialect: Any) -> list[float] | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            # pgvector returns a list-like (or numpy array); normalise to list.
            return [float(v) for v in value]
        return list(pickle.loads(value))

    class comparator_factory(TypeDecorator.Comparator):  # type: ignore[type-arg]
        """Expose the pgvector distance operators on the mapped column.

        Mirrors ``pgvector.sqlalchemy.Vector``'s own comparator so
        ``Chunk.embedding.cosine_distance(vec)`` compiles to ``embedding <=> :vec``.
        """

        def cosine_distance(self, other: Any) -> Any:
            return self.op("<=>", return_type=Float)(other)

        def l2_distance(self, other: Any) -> Any:
            return self.op("<->", return_type=Float)(other)

        def max_inner_product(self, other: Any) -> Any:
            return self.op("<#>", return_type=Float)(other)
