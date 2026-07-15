"""Tests for :class:`app.embeddings.openrouter.OpenRouterEmbedder`.

Mirrors ``tests/test_embeddings_gemini.py`` in structure (mocked ``httpx``
transport, no network, no key required) but pins the OpenAI-compatible wire
format, which is deliberately NOT the Gemini one:

* Request is ``{"model", "input", "dimensions"}`` to ``{base}/embeddings`` with a
  ``Authorization: Bearer`` header — and carries **no** ``taskType`` (OpenAI has no
  query/document task distinction; query and document share one vector space).
* Response is ``{"data": [{"embedding": [...], "index": N}]}``; items are ordered by
  ``index`` before being paired with inputs, so a reordered response can never
  misalign a stored vector with the wrong chunk.
* Issue #50 discipline: the upstream error body and the Bearer key never appear in
  the raised exception.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.embeddings.errors import EmbedderUnavailable
from app.embeddings.openrouter import OpenRouterEmbedder

_API_BASE = "https://openrouter.ai/api/v1"


def _client(handler, *, dim: int = 4, max_retries: int = 2) -> OpenRouterEmbedder:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=_API_BASE,
    )
    return OpenRouterEmbedder(
        model="openai/text-embedding-3-small",
        api_key="or-test",
        api_base=_API_BASE,
        dim=dim,
        timeout_seconds=5.0,
        max_retries=max_retries,
        retry_backoff_seconds=0.0,  # keep retry tests instant
        http_client=http_client,
    )


def _data(vectors: list[list[float]]) -> dict:
    """Build a well-formed OpenAI-style embeddings response body."""
    return {
        "object": "list",
        "model": "openai/text-embedding-3-small",
        "data": [
            {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


# ---------------------------------------------------------------------------
# Happy path — wire format
# ---------------------------------------------------------------------------


async def test_embed_query_happy_path() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        body = json.loads(request.content)
        seen["input"] = body["input"]
        seen["dimensions"] = body["dimensions"]
        seen["model"] = body["model"]
        seen["has_task_type"] = "taskType" in body or "task_type" in body
        return httpx.Response(200, json=_data([[0.1, 0.2, 0.3, 0.4]]))

    client = _client(handler, dim=4)
    try:
        vector = await client.embed("what is the rate limit?")
    finally:
        await client.aclose()

    assert vector == [0.1, 0.2, 0.3, 0.4]
    assert str(seen["url"]).endswith("/embeddings")
    assert seen["auth"] == "Bearer or-test"
    # A single query is sent as a one-element list; dimensions is explicit.
    assert seen["input"] == ["what is the rate limit?"]
    assert seen["dimensions"] == 4
    assert seen["model"] == "openai/text-embedding-3-small"
    # OpenAI has no query/document task type — it must NOT be sent.
    assert seen["has_task_type"] is False


async def test_embed_documents_batch_happy_path() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["input"] = body["input"]
        seen["has_task_type"] = "taskType" in body
        return httpx.Response(200, json=_data([[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]))

    client = _client(handler, dim=4)
    try:
        vectors = await client.embed_documents(["doc one", "doc two"])
    finally:
        await client.aclose()

    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    assert seen["input"] == ["doc one", "doc two"]
    assert seen["has_task_type"] is False


async def test_embed_documents_response_reordered_by_index() -> None:
    """A response whose data items are out of order is realigned by ``index``.

    OpenAI does not guarantee response order; pairing by position would silently
    map each vector to the WRONG input (corrupting every retrieval). The embedder
    must sort by the per-item ``index``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Return the two items in REVERSE order, each tagged with its true index.
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [1.0, 1.0, 1.0, 1.0]},
                    {"index": 0, "embedding": [0.0, 0.0, 0.0, 0.0]},
                ]
            },
        )

    client = _client(handler, dim=4)
    try:
        vectors = await client.embed_documents(["first", "second"])
    finally:
        await client.aclose()

    # Input 0 → the index-0 vector, input 1 → the index-1 vector, despite the
    # reversed wire order.
    assert vectors == [[0.0, 0.0, 0.0, 0.0], [1.0, 1.0, 1.0, 1.0]]


async def test_embed_documents_splits_into_provider_safe_batches(monkeypatch) -> None:
    """More texts than the batch cap → multiple requests, concatenated in order."""
    import app.embeddings.openrouter as orm

    monkeypatch.setattr(orm, "_EMBED_BATCH_SIZE", 2)
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(json.loads(request.content)["input"])
        calls.append(n)
        base = sum(calls[:-1])  # items embedded before this batch
        return httpx.Response(200, json=_data([[float(base + i), 0.0, 0.0, 0.0] for i in range(n)]))

    client = _client(handler, dim=4)
    try:
        vectors = await client.embed_documents(["a", "b", "c", "d", "e"])
    finally:
        await client.aclose()

    assert calls == [2, 2, 1]  # 5 texts, cap 2
    assert len(vectors) == 5
    assert [v[0] for v in vectors] == [0.0, 1.0, 2.0, 3.0, 4.0]  # order preserved


async def test_embed_documents_empty_batch_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no HTTP call should be made for an empty batch")

    client = _client(handler)
    try:
        assert await client.embed_documents([]) == []
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Failure / edge
# ---------------------------------------------------------------------------


async def test_embed_count_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Asked for 2, got 1 back.
        return httpx.Response(200, json=_data([[0.1, 0.2, 0.3, 0.4]]))

    client = _client(handler, dim=4)
    try:
        with pytest.raises(EmbedderUnavailable, match="count did not match"):
            await client.embed_documents(["a", "b"])
    finally:
        await client.aclose()


async def test_embed_missing_data_array_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    client = _client(handler, dim=4)
    try:
        with pytest.raises(EmbedderUnavailable, match="missing 'data'"):
            await client.embed("q")
    finally:
        await client.aclose()


async def test_embed_wrong_dimension_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_data([[0.1, 0.2, 0.3]]))  # 3, asked for 4

    client = _client(handler, dim=4)
    try:
        with pytest.raises(EmbedderUnavailable, match="expected 4"):
            await client.embed("q")
    finally:
        await client.aclose()


async def test_embed_non_numeric_value_raises_generic_no_leak() -> None:
    secret_token = "NaN-SECRET-42"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_data([[0.1, secret_token, 0.3, 0.4]]))

    client = _client(handler, dim=4)
    try:
        with pytest.raises(EmbedderUnavailable) as info:
            await client.embed("q")
    finally:
        await client.aclose()

    assert secret_token not in str(info.value)
    assert "non-numeric" in str(info.value)


async def test_embed_empty_input_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("empty input must fail before any HTTP call")

    client = _client(handler)
    try:
        with pytest.raises(EmbedderUnavailable, match="empty"):
            await client.embed("   ")
    finally:
        await client.aclose()


async def test_embed_oversized_input_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("oversized input must fail before any HTTP call")

    client = _client(handler)
    try:
        with pytest.raises(EmbedderUnavailable, match="exceeds"):
            await client.embed("x" * 9000)
    finally:
        await client.aclose()


async def test_embed_timeout_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    client = _client(handler, max_retries=1)
    try:
        with pytest.raises(EmbedderUnavailable, match="timed out"):
            await client.embed("q")
    finally:
        await client.aclose()


async def test_embed_transport_error_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = _client(handler)
    try:
        with pytest.raises(EmbedderUnavailable, match="transport error"):
            await client.embed("q")
    finally:
        await client.aclose()


async def test_transient_error_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=_data([[0.1, 0.2, 0.3, 0.4]]))

    client = _client(handler, dim=4, max_retries=2)
    try:
        vector = await client.embed("q")
    finally:
        await client.aclose()

    assert vector == [0.1, 0.2, 0.3, 0.4]
    assert calls["n"] == 2  # one retry


async def test_fatal_status_is_not_retried_even_with_retries_available() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    client = _client(handler, dim=4, max_retries=2)
    try:
        with pytest.raises(EmbedderUnavailable, match="returned 400"):
            await client.embed("q")
    finally:
        await client.aclose()

    assert calls["n"] == 1  # raised immediately; retry budget untouched


async def test_retry_applies_exponential_backoff(monkeypatch) -> None:
    import app.embeddings.openrouter as orm

    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(orm.asyncio, "sleep", _fake_sleep)

    n = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        n["i"] += 1
        if n["i"] <= 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json=_data([[0.1, 0.2, 0.3, 0.4]]))

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=_API_BASE)
    client = OpenRouterEmbedder(
        model="openai/text-embedding-3-small",
        api_key="or-test",
        api_base=_API_BASE,
        dim=4,
        timeout_seconds=5.0,
        max_retries=2,
        retry_backoff_seconds=0.25,
        http_client=http_client,
    )
    try:
        await client.embed("q")
    finally:
        await client.aclose()

    assert sleeps == [0.25, 0.5]


# ---------------------------------------------------------------------------
# Issue #50 — no upstream error-body / Bearer-key leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_error_body_and_key_never_leak(status: int, caplog) -> None:
    secret_body = "UPSTREAM SECRET billing project 12345"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=secret_body)

    client = _client(handler, dim=4, max_retries=0)
    with caplog.at_level("WARNING", logger="citevyn.embeddings"):
        try:
            with pytest.raises(EmbedderUnavailable) as info:
                await client.embed("q")
        finally:
            await client.aclose()

    # Neither the upstream body nor the Bearer key reaches the client-visible error.
    assert secret_body not in str(info.value)
    assert "or-test" not in str(info.value)
    assert str(info.value) == f"OpenRouter embeddings returned {status}"
    # The body IS captured server-side for debugging; the auth header is NOT logged.
    assert any(secret_body in str(rec.__dict__.get("body", "")) for rec in caplog.records)
    assert all("or-test" not in str(rec.__dict__) for rec in caplog.records)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_missing_key_raises_eagerly() -> None:
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        OpenRouterEmbedder(
            model="openai/text-embedding-3-small",
            api_key=None,
            api_base=_API_BASE,
            dim=4,
            timeout_seconds=5.0,
        )
