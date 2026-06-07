# Backend Component Architecture Diagram

## Purpose

Show the logical backend components that handle authentication, routing, retrieval, generation, citation validation, ingestion, evaluation, and audit logging.

## Scope

This diagram focuses on backend internals. It does not show deployment infrastructure.

## Saved File Path

`diagrams/03-backend-component-architecture.md`

## Mermaid Diagram

```mermaid
flowchart TD
    api[API Layer]

    subgraph security["Security and Controls"]
        auth[Demo Auth]
        rate[Rate Limiter]
        audit[Audit Logger]
        redaction[Log Redaction]
    end

    subgraph request["Request Pipeline"]
        domain[Domain Guardrail]
        session[Session Manager]
        intent[Intent Router]
        fallback[No Answer Gate]
    end

    subgraph retrieval["Retrieval Pipeline"]
        cache[Cache Service]
        exact[Exact Lookup]
        keyword[Keyword Search]
        vector[Vector Search]
        rerank[Reranker]
    end

    subgraph generation["Answer Quality"]
        answer[Grounded Generator]
        cite[Citation Validator]
        confidence[Confidence Gate]
    end

    subgraph ingestion["Ingestion Pipeline"]
        fetch[Source Fetcher]
        parse[Parser]
        chunk[Contextual Chunker]
        terms[Term Extractor]
        embedder[Embedding Service]
        indexer[Index Builder]
    end

    subgraph evaluation["Evaluation Pipeline"]
        golden[Golden Test Runner]
        scorer[Quality Scorer]
        promote[Index Promotion]
    end

    stores[(PostgreSQL and pgvector)]
    redis[(Redis)]
    llm[Generation Model]
    embeddings[Embedding Model]

    api --> auth
    auth --> rate
    rate --> domain
    domain --> session
    session --> intent

    intent --> cache
    intent --> exact
    intent --> keyword
    intent --> vector
    exact --> stores
    keyword --> stores
    vector --> stores
    cache --> redis

    keyword --> rerank
    vector --> rerank
    exact --> rerank

    rerank --> answer
    answer --> llm
    answer --> cite
    cite --> confidence
    confidence --> fallback
    fallback --> api

    fetch --> parse
    parse --> chunk
    chunk --> terms
    terms --> embedder
    embedder --> embeddings
    embedder --> indexer
    indexer --> stores

    golden --> scorer
    scorer --> promote
    promote --> stores
    golden --> stores
    scorer --> llm

    api --> audit
    fetch --> audit
    promote --> audit
    audit --> redaction
    redaction --> stores
```

## Short Explanation

The backend is split into clear pipelines. The request pipeline handles security, domain scope, routing, retrieval, generation, citation validation, and no-answer behavior. Ingestion and evaluation are separate pipelines so bad sources or bad candidate indexes do not affect live answers.

## Key Assumptions

1. Domain guardrail runs before retrieval and generation.
2. Exact lookup is first-class and not delegated to vector search.
3. Citation validation happens before returning factual answers.
4. Candidate indexes require evaluation before promotion.
5. Audit logging is used for admin actions and critical user flows.

## Open Questions

1. Should domain guardrail be rule-based, model-based, or hybrid?
2. What reranker should be used in MVP?
3. What strictness level should citation validation enforce initially?
