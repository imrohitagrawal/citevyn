# Deployment Architecture Diagram

## Purpose

Show the MVP deployment architecture for a local or single-VM Docker Compose environment.

## Scope

This diagram covers deployment topology, service boundaries, data stores, external APIs, and basic trust boundaries. It does not define cloud-specific infrastructure.

## Saved File Path

`diagrams/06-deployment-architecture.md`

## Mermaid Diagram

```mermaid
flowchart TD
    user[User Browser]
    admin[Admin Browser]

    subgraph internet["Internet or Local Network"]
        user
        admin
    end

    subgraph host["Docker Compose Host"]
        proxy[Reverse Proxy]
        frontend[Frontend Container]
        backend[Backend API Container]
        worker[Worker Container]
        evaluator[Evaluation Container]
        postgres[(PostgreSQL and pgvector)]
        redis[(Redis)]
        logOut[JSON Logs]
    end

    subgraph external["External Services"]
        docs[Official Docs]
        llm[Generation Model API]
        embed[Embedding Model API]
    end

    user -->|HTTPS| proxy
    admin -->|HTTPS| proxy
    proxy --> frontend
    frontend -->|API calls| backend

    backend -->|SQL and vector search| postgres
    backend -->|cache and limits| redis
    backend -->|generation| llm
    backend -->|query embeddings| embed
    backend --> logOut

    backend -->|start ingestion| worker
    backend -->|start evaluation| evaluator

    worker -->|fetch allowlisted docs| docs
    worker -->|store index data| postgres
    worker -->|document embeddings| embed
    worker --> logOut

    evaluator -->|read cases and index| postgres
    evaluator -->|judge answers if needed| llm
    evaluator --> logOut
```

## Short Explanation

The MVP runs as Docker Compose on a local machine or single VM. The reverse proxy fronts the web UI and backend. The backend handles user traffic, while worker and evaluator containers handle ingestion and test runs. Data stores remain separate from business services.

## Key Assumptions

1. Docker Compose is sufficient for controlled demo traffic.
2. A reverse proxy is optional for local-only development but recommended for cloud demo.
3. PostgreSQL and Redis run as containers for MVP.
4. Model APIs and official documentation sources are external network dependencies.
5. Kubernetes is deferred.

## Open Questions

1. Will the first demo run locally or on a cloud VM?
2. Which reverse proxy will be used?
3. How will demo secrets be injected and rotated?
4. Should logs be shipped to an external service in the first demo?
