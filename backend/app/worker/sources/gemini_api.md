# Gemini API Reference

## Overview

The Gemini API is Google's REST interface for generating text, structured output,
and embeddings from Gemini models. Requests are JSON over HTTPS and are
authenticated with an API key. Gemini is a family of large language models (LLMs),
so the Gemini API is an LLM-based service. This page is an original, paraphrased summary of the
official Gemini API documentation, written for use as a grounded demo corpus.

## Authentication

Pass your Gemini API key in the x-goog-api-key header on every request. The Gemini
CLI also accepts the key from a credentials file or the GEMINI_API_KEY environment
variable. Keys are secret; scope them narrowly and rotate them if they may have
been exposed.

## Models

The --model flag selects the Gemini model used for a request. Choose a Pro-tier
model for the hardest reasoning and a Flash-tier model for fast, high-volume, or
cost-sensitive work. Pin an explicit model version in production so behavior does
not change under you when a default alias moves.

## Generating content

Send a request to the model's generateContent method with your prompt in the
contents field and optional generation settings such as the maximum output tokens
and temperature. The response returns the generated candidates plus usage metadata
describing how many tokens the prompt and the answer consumed.

## Embeddings

The Gemini API also produces embedding vectors through the embedContent and
batchEmbedContents methods. Embeddings map text to a fixed-length vector so you can
measure semantic similarity, power retrieval, and cluster related documents.
Requesting a smaller output dimensionality trades a little quality for less storage
and faster search.

## Thinking

Some Gemini models support a thinking budget that lets the model reason internally
before answering. Raising the budget can improve accuracy on hard problems at the
cost of latency and tokens; setting it to zero spends the whole budget on the
visible answer, which suits short, extractive responses.

## Errors

The Gemini API uses standard HTTP status codes. A 400 signals a malformed request,
a 401 or 403 signals an authentication or permission problem, a 429 signals rate
limiting, and 5xx signals a transient server error. Retry 429 and 5xx with backoff;
fix 400 and 401 in the caller.
