# Claude API Reference

## Overview

The Claude API is a REST interface for sending messages to Claude models and
receiving generated responses. Requests are JSON over HTTPS, and every request is
authenticated and rate limited per organization. Claude is a family of large
language models (LLMs), so the Claude API is an LLM-based service. This page is an original,
paraphrased summary of the official Anthropic API documentation, written for use
as a grounded demo corpus.

## Authentication

Pass your Claude API key in the x-api-key header on every request. If the header
is absent, the Claude SDKs fall back to reading the key from the ANTHROPIC_API_KEY
environment variable. Keys are secret: never commit them to source control and
never place them in query strings, which can be logged by intermediaries.

## Rate limits

The Claude API enforces a default rate limit of 50 requests per minute for
self-serve accounts. The CLAUDE_API_RATE_LIMIT environment variable can override
the client-side ceiling the SDK will attempt, but the server-side organization
limit still applies. When you exceed the limit the API returns HTTP 429; clients
should honor the retry-after hint and back off exponentially rather than retrying
immediately.

## Models

The --model flag (or the "model" field in the JSON body) selects which Claude
model handles a request. Choose a larger model for harder reasoning and a smaller
model for latency-sensitive, high-volume calls. Model identifiers are versioned,
so pin an explicit version in production rather than relying on an alias that may
move over time.

## Messages

A request carries a list of messages, each with a role of "user" or "assistant",
plus an optional system prompt that steers behavior. The response returns the
assistant message along with token usage counts for the input and output, which
you can use to estimate cost and stay within budget.

## Errors

Error responses use standard HTTP status codes: 400 for a malformed request, 401
for a missing or invalid key, 429 for rate limiting, and 5xx for a transient
server problem. The body contains a machine-readable error type and a human
message. Treat 5xx and 429 as retryable; treat 400 and 401 as caller bugs to fix.

## Streaming

Set the stream option to receive the response as server-sent events instead of a
single JSON body. Streaming lowers time-to-first-token for interactive UIs. Each
event carries an incremental chunk of the answer; concatenate the chunks in order
to reconstruct the full message.
