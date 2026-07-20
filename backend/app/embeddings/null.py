"""The embedder that writes no vectors at all.

:class:`NullEmbedder` exists for exactly one caller: the demo/bootstrap seeder
(``db/seed/seed_catalog.py``) running under the default **stub** provider.

Why not just use :class:`~app.embeddings.stub.StubEmbedder` there? Because the
stub's vectors are *deterministic but meaningless* — it hash-buckets a SHA-256
digest, so two chunks about the same topic are as far apart as two unrelated
ones. Persisting them into the index the demo serves is worse than persisting
nothing:

* The demo API is configured with that same stub, so the Tier-3 provenance gate
  (:func:`app.embeddings.factory.is_index_embedder_mismatch`) sees stamp ==
  config and **enables** the pgvector arm, which then ranks by hash distance and
  hands the LLM confidently mis-ordered chunks.
* ``GET /health/index`` counts embedded chunks, so it would report the arm
  ``healthy`` while it is returning garbage
  (:func:`app.services.index_health.derive_vector_arm_status`).

A dead arm degrades gracefully — :class:`app.retrieval.vector.VectorRetriever`
filters on ``Chunk.embedding.is_not(None)``, so retrieval falls back to the
exact + keyword arms and the operator gets an honest ``dead`` on
``/health/index``. A live-with-nonsense arm has no fallback and no signal.

So the bootstrap must never *write* stub vectors. Stripping them afterwards
(the approach this replaces) cannot close the hole: ``app.worker.cli.drive``
commits each source as it goes, and on a re-seed ``v1`` is already ``active``,
so a query landing mid-seed reads vectors that the post-pass has not reached
yet. Not writing them has no window.

The class satisfies :class:`~app.embeddings.protocol.DocumentEmbedder` — the
write-path half of the embedder seam — and deliberately NOT the full
:class:`~app.embeddings.protocol.Embedder`: an embedder that cannot produce a
query vector must never be reachable from the read path, and omitting
``embed`` makes that a type error rather than a runtime surprise.
"""

from __future__ import annotations


class NullEmbedder:
    """Returns one ``None`` per input text: chunks are persisted unembedded."""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        """The vector column's dimension.

        Reported truthfully even though nothing is written, because the runner
        forwards it to the ``IndexVersion`` stamp. It is inert on its own: the
        provenance gate keys off ``provider``, which the bootstrap leaves NULL
        ("unknown provenance ⇒ allow"), and the health check keys off the count
        of embedded chunks, which is zero.
        """
        return self._dim

    async def embed_documents(self, texts: list[str]) -> list[None]:
        """One ``None`` per text, in order.

        The length must match: ``IngestionRunner._materialize_chunks`` zips the
        result against the chunk drafts with ``strict=True``, so a short list
        would raise instead of silently dropping chunks.
        """
        return [None] * len(texts)
