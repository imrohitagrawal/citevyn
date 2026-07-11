"""Tests for :class:`app.embeddings.gemini.GeminiEmbedder`.

Mirrors ``tests/test_llm_gemini_openrouter.py``: the provider HTTP is mocked with
:class:`httpx.MockTransport` so there is no network and no key is required. We pin:

* Happy path — ``embed`` / ``embed_documents`` return ``list[float]`` of the
  configured dim, with the right endpoint, headers, task type, and
  outputDimensionality.
* Failure/edge — timeout, transport error, HTTP error, empty/oversized input,
  wrong-dimension body, batch count mismatch.
* Issue #50 — the upstream error body is logged server-side but never appears in
  the raised exception message.
* Missing key raises eagerly at construction.
"""

from __future__ import annotations

import httpx
import pytest

from app.embeddings.errors import EmbedderUnavailable
from app.embeddings.gemini import GeminiEmbedder

_API_BASE = "https://generativelanguage.googleapis.com"


def _client(handler, *, dim: int = 4, max_retries: int = 2) -> GeminiEmbedder:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=_API_BASE,
    )
    return GeminiEmbedder(
        model="gemini-embedding-001",
        api_key="em-test",
        api_base=_API_BASE,
        dim=dim,
        timeout_seconds=5.0,
        max_retries=max_retries,
        http_client=http_client,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_embed_query_happy_path() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("x-goog-api-key")
        import json

        body = json.loads(request.content)
        seen["taskType"] = body["taskType"]
        seen["outputDimensionality"] = body["outputDimensionality"]
        seen["model"] = body["model"]
        return httpx.Response(200, json={"embedding": {"values": [0.1, 0.2, 0.3, 0.4]}})

    client = _client(handler, dim=4)
    try:
        vector = await client.embed("what is the rate limit?")
    finally:
        await client.aclose()

    assert vector == [0.1, 0.2, 0.3, 0.4]
    assert "embedContent" in str(seen["url"])
    assert seen["api_key"] == "em-test"
    assert seen["taskType"] == "RETRIEVAL_QUERY"
    assert seen["outputDimensionality"] == 4
    assert seen["model"] == "models/gemini-embedding-001"


async def test_embed_documents_batch_happy_path() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["url"] = str(request.url)
        body = json.loads(request.content)
        seen["count"] = len(body["requests"])
        seen["taskType"] = body["requests"][0]["taskType"]
        return httpx.Response(
            200,
            json={
                "embeddings": [
                    {"values": [0.1, 0.2, 0.3, 0.4]},
                    {"values": [0.5, 0.6, 0.7, 0.8]},
                ]
            },
        )

    client = _client(handler, dim=4)
    try:
        vectors = await client.embed_documents(["doc one", "doc two"])
    finally:
        await client.aclose()

    assert vectors == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    assert "batchEmbedContents" in str(seen["url"])
    assert seen["count"] == 2
    assert seen["taskType"] == "RETRIEVAL_DOCUMENT"


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


async def test_embed_wrong_dimension_body_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Server returns 3 values but the client asked for dim=4.
        return httpx.Response(200, json={"embedding": {"values": [0.1, 0.2, 0.3]}})

    client = _client(handler, dim=4)
    try:
        with pytest.raises(EmbedderUnavailable, match="expected 4"):
            await client.embed("q")
    finally:
        await client.aclose()


async def test_embed_documents_count_mismatch_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Asked for 2, got 1 back.
        return httpx.Response(200, json={"embeddings": [{"values": [0.1, 0.2, 0.3, 0.4]}]})

    client = _client(handler, dim=4)
    try:
        with pytest.raises(EmbedderUnavailable, match="count did not match"):
            await client.embed_documents(["a", "b"])
    finally:
        await client.aclose()


async def test_transient_error_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json={"embedding": {"values": [0.1, 0.2, 0.3, 0.4]}})

    client = _client(handler, dim=4, max_retries=2)
    try:
        vector = await client.embed("q")
    finally:
        await client.aclose()

    assert vector == [0.1, 0.2, 0.3, 0.4]
    assert calls["n"] == 2  # one retry


# ---------------------------------------------------------------------------
# Issue #50 — no upstream error-body leak
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_error_body_is_logged_not_in_exception(status: int, caplog) -> None:
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

    # The upstream body must NOT be in the client-visible exception.
    assert secret_body not in str(info.value)
    assert str(info.value) == f"Gemini embeddings returned {status}"
    # ...but it IS captured server-side for debugging.
    assert any(secret_body in str(rec.__dict__.get("body", "")) for rec in caplog.records)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_missing_key_raises_eagerly() -> None:
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiEmbedder(
            model="gemini-embedding-001",
            api_key=None,
            api_base=_API_BASE,
            dim=4,
            timeout_seconds=5.0,
        )
