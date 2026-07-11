"""Deterministic offline embedder.

The default embedder, exactly like :class:`app.llm.stub.StubLLMClient` is the
default LLM. It requires no API key and no network, so the whole test suite stays
hermetic and local development works out of the box.

Design
------
* **Deterministic:** same text → same vector, every run. This is what makes the
  retrieval tests reproducible.
* **Unit-normalised:** the vector is scaled to length 1 so cosine distance against
  the pgvector ``vector`` column is well defined (a query and an identical document
  chunk score distance ~0).
* **Not semantic:** it hash-buckets the SHA-256 digest into ``dim`` slots. It
  distinguishes distinct chunks but carries no meaning — which is the entire reason
  #51 wires a real provider. The stub exists only so tests and offline runs work.
* Async to match the :class:`Embedder` protocol; there is no real I/O.
"""

from __future__ import annotations

import hashlib
import math


class StubEmbedder:
    """Deterministic, in-process embedder (no key, no network)."""

    def __init__(self, dim: int) -> None:
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def _vector(self, text: str) -> list[float]:
        """Deterministic unit-normalised vector for ``text``.

        Empty text embeds to the zero vector (its norm is undefined; a zero
        vector is the honest "no signal" value and sorts to maximum cosine
        distance from every real vector).
        """
        if not text:
            return [0.0] * self._dim
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        raw: list[float] = []
        i = 0
        while len(raw) < self._dim:
            # Centre each byte on 0 so the vector has both signs, then normalise.
            raw.append((digest[i % len(digest)] - 128) / 128.0)
            i += 1
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]

    async def embed(self, text: str) -> list[float]:
        return self._vector(text)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]
