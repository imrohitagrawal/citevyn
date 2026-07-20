"""The :class:`Embedder` seam.

One structural protocol satisfied by every embedder (stub + real). It mirrors
:mod:`app.llm.protocol`: implementations satisfy it structurally (no inheritance),
so tests can inject a fake without importing a base class.

Two methods, deliberately asymmetric:

* :meth:`embed` — embed a **query** (the user's question). Real providers tag it
  ``RETRIEVAL_QUERY``.
* :meth:`embed_documents` — embed a **batch of documents** (chunks at ingest). Real
  providers tag it ``RETRIEVAL_DOCUMENT`` and batch the HTTP call.

Retrieval-optimised embedders (Gemini) produce measurably better matches when the
query and the document are embedded with their respective task types, which is why
the write path and the read path call different methods. The stub ignores the
distinction and returns the same deterministic vector either way, so hermetic tests
stay simple.

The contract is ``list[float]`` of length :attr:`dim`. Retrieval ranks by cosine
distance (pgvector's ``<=>``), which is scale-invariant, so implementations need not
return unit-length vectors — the :class:`~app.embeddings.stub.StubEmbedder`
normalises for determinism, while a real provider (Gemini) returns provider-scaled
values. Do not rely on unit norm downstream (e.g. raw inner product).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class DocumentEmbedder(Protocol):
    """The write path's half of the seam: batch-embed documents, that is all.

    Split out of :class:`Embedder` because the ingestion runner never embeds a
    query — it only needs ``dim`` (for the ``IndexVersion`` stamp) and
    ``embed_documents``. Narrowing the runner's parameter to this protocol is
    what lets the demo/bootstrap seeder pass an
    :class:`~app.embeddings.null.NullEmbedder`, which writes no vectors at all
    and therefore cannot produce a query vector either.

    The return element is ``list[float] | None`` rather than ``list[float]``:
    ``None`` means "persist this chunk with a NULL embedding", the state the
    read path already short-circuits on. ``Sequence`` (covariant) rather than
    ``list`` (invariant) so a real :class:`Embedder` returning
    ``list[list[float]]`` satisfies this protocol unchanged.
    """

    @property
    def dim(self) -> int:
        """The vector dimension this embedder produces."""
        ...

    async def embed_documents(self, texts: list[str]) -> Sequence[list[float] | None]:
        """Embed a batch of document strings, preserving input order and length."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Compute fixed-dimension embedding vectors for text."""

    @property
    def dim(self) -> int:
        """The vector dimension this embedder produces."""
        ...

    async def embed(self, text: str) -> list[float]:
        """Embed a single query string. Returns a ``list[float]`` of length :attr:`dim`."""
        ...

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document strings, preserving input order.

        Returns one ``list[float]`` (length :attr:`dim`) per input text.
        """
        ...
