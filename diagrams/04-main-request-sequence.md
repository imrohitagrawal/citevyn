# Main Request Sequence Diagram

## Purpose

Show the main runtime sequence for a user question, including auth, routing, retrieval, answer generation, citation validation, observability, and fallback behavior.

## Scope

This diagram covers the primary user journey for asking a question through the chat UI. It includes cache, unsupported, and weak-evidence branches.

## Saved File Path

`diagrams/04-main-request-sequence.md`

## Mermaid Diagram

```mermaid
sequenceDiagram
    autonumber
    participant User
    participant UI as Web Chat UI
    participant API as Backend API
    participant Auth as Auth and Rate Limit
    participant Guard as Domain Guardrail
    participant Router as Intent Router
    participant Cache as Cache Service
    participant Retrieval as Retrieval Service
    participant Store as PostgreSQL and pgvector
    participant LLM as Generation Model
    participant Cite as Citation Validator
    participant Obs as Observability

    User->>UI: Ask question
    UI->>API: POST message
    API->>Auth: Validate access and quota
    Auth-->>API: Allowed

    API->>Guard: Classify product scope

    alt Unsupported scope
        Guard-->>API: Unsupported
        API->>Obs: Log unsupported response
        API-->>UI: Scoped refusal
        UI-->>User: Show unsupported message
    else Supported scope
        Guard-->>API: Product area
        API->>Router: Classify intent
        Router->>Cache: Check eligible cache

        alt Cache hit
            Cache-->>Router: Cached answer with citations
            Router-->>API: Cached response
            API->>Obs: Log cache hit
            API-->>UI: Answer with citations
            UI-->>User: Show answer
        else Cache miss
            Router->>Retrieval: Retrieve evidence
            Retrieval->>Store: Exact, keyword, vector search
            Store-->>Retrieval: Candidate chunks
            Retrieval-->>Router: Ranked evidence

            alt Weak evidence
                Router-->>API: No reliable answer
                API->>Obs: Log no-answer
                API-->>UI: No-answer response
                UI-->>User: Show no-answer message
            else Evidence available
                Router->>LLM: Generate grounded answer
                LLM-->>Router: Draft answer
                Router->>Cite: Validate citation support

                alt Citation validation failed
                    Cite-->>Router: Failed
                    Router-->>API: No-answer response
                    API->>Obs: Log citation failure
                    API-->>UI: No-answer response
                    UI-->>User: Show no-answer message
                else Citation validation passed
                    Cite-->>Router: Passed
                    Router-->>API: Answer and citations
                    API->>Obs: Log trace and metrics
                    API-->>UI: Answer with citations
                    UI-->>User: Show answer
                end
            end
        end
    end
```

## Short Explanation

The runtime path protects answer quality before generation and after generation. Unsupported queries are refused early. Cache hits avoid unnecessary LLM calls. Weak evidence and citation failures produce no-answer responses instead of unsupported guesses.

## Key Assumptions

1. Domain classification happens before retrieval.
2. Cache entries require citations and source-version validation.
3. Weak evidence is treated as no-answer.
4. Citation validation failure blocks factual answers.
5. Observability records all major outcomes.

## Open Questions

1. Should answers stream in MVP or return after full validation?
2. Should citation validation use deterministic rules, an evaluator model, or both?
3. Should cache hits still run lightweight citation freshness checks?
