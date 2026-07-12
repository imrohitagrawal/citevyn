/**
 * Type definitions for the CiteVyn REST API.
 *
 * This module is the single source of truth for the API response
 * shapes the UI consumes. The backend's pydantic models (see
 * ``backend/app/api/routes/messages.py`` and ``search.py``) are the
 * authoritative source — when those change, the fields here must
 * change in lockstep. The orchestrator response is a
 * ``dict[str, Any]`` in Python; the field order and naming below
 * mirrors the construction sites in
 * ``backend/app/answer/orchestrator.py`` and
 * ``backend/app/answer/no_answer.py``.
 */

// ---------------------------------------------------------------------------
// Common envelope
// ---------------------------------------------------------------------------

/** A request id is always present on success and error responses. */
export type RequestId = string;

/** A session id (UUID in the backend; rendered as a string in the UI). */
export type SessionId = string;

/** A message id (UUID in the backend; rendered as a string in the UI). */
export type MessageId = string;

/** A document or chunk id (UUID in the backend; rendered as a string). */
export type UuidString = string;

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

/**
 * Body of ``POST /v1/sessions``.
 *
 * ``user_id`` is required by the backend; ``channel`` is optional and
 * defaults to ``"chat"``. The backend has no enum constraint on
 * ``channel`` in the MVP — it is stored as a string.
 */
export interface CreateSessionRequest {
  user_id: string;
  channel?: string;
}

/** Response of ``POST /v1/sessions``. */
export interface CreateSessionResponse {
  request_id: RequestId;
  session_id: SessionId;
  /** ISO-8601 timestamp when the session expires. */
  expires_at: string;
}

// ---------------------------------------------------------------------------
// Citations and answers
// ---------------------------------------------------------------------------

/**
 * One citation, as emitted by
 * :func:`app.retrieval.types.chunk_to_citation` in the backend.
 *
 * The chunk_id is the most useful field for support / debugging —
 * it links the citation to the exact indexed chunk.
 */
export interface Citation {
  source_name: string;
  title: string;
  url: string;
  chunk_id: UuidString;
}

/**
 * Confidence of the answer. The backend's :class:`Confidence` enum
 * is a string enum; the values are the only legal ones.
 */
export type Confidence = "none" | "low" | "medium" | "high";

/**
 * Public-facing retrieval strategy label. Matches
 * :class:`app.answer.orchestrator.RetrievalStrategy`.
 */
export type RetrievalStrategy = "none" | "cache" | "exact_lookup" | "hybrid_reranked";

/**
 * Intent label, mirroring :class:`app.routing.intent.Intent` in the
 * backend. ``unsupported`` is the one the UI cares about most —
 * the chat surfaces a dedicated empty state when it comes back.
 */
export type Intent =
  | "faq"
  | "how_to"
  | "troubleshooting"
  | "lookup"
  | "smalltalk"
  | "unsupported";

/** Domain label, mirroring :class:`app.guardrails.domain.Domain`. */
export type Domain =
  | "claude_api"
  | "claude_code"
  | "codex"
  | "gemini_api"
  | "citevyn"
  | "unsupported";

/**
 * Response of ``POST /v1/sessions/{id}/messages``.
 *
 * Every response carries the same fields; the difference between
 * "grounded", "no-answer", "unsupported", and "cache hit" is encoded
 * in the ``unsupported``, ``no_answer``, ``cache_hit`` flags and
 * the ``retrieval_strategy`` value. The UI does not need to
 * special-case the wire format — just render based on those flags.
 */
export interface AskResponse {
  request_id: RequestId;
  message_id: MessageId;
  answer: string;
  citations: Citation[];
  domain: Domain;
  intent: Intent;
  confidence: Confidence;
  cache_hit: boolean;
  retrieval_strategy: RetrievalStrategy;
  unsupported: boolean;
  no_answer: boolean;
  /** Index hash of the source content. Empty string for unsupported. */
  source_version_hash: string;
  /** Answer-policy version that produced the response. */
  answer_policy_version: string;
}

// ---------------------------------------------------------------------------
// Exact search
// ---------------------------------------------------------------------------

/**
 * Term types emitted by the backend's exact-term index. Mirrors
 * :class:`app.models.enums.TermType`. The UI displays each as a
 * coloured badge; the mapping is in ``./format.ts``.
 */
export type TermType =
  | "flag"
  | "command"
  | "config_key"
  | "model_name"
  | "api_parameter"
  | "error_message"
  | "environment_variable"
  | "file_name"
  | "slash_command";

/**
 * Body of ``POST /v1/search/exact``. ``product_area`` is required
 * so the lookup is scoped to a single product — the same flag
 * name in two products can mean different things.
 */
export interface ExactSearchRequest {
  term: string;
  product_area: Domain;
  max_results?: number;
}

/** One hit in the exact-search response. */
export interface ExactSearchHit {
  term_id: UuidString;
  term_text: string;
  term_type: TermType;
  product_area: Domain;
  document_id: UuidString;
  chunk_id: UuidString;
  index_version: string;
  score: number;
}

/** Response of ``POST /v1/search/exact``. */
export interface ExactSearchResponse {
  request_id: RequestId;
  query: string;
  product_area: Domain;
  index_version: string;
  total: number;
  hits: ExactSearchHit[];
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

/** Response of ``GET /health``. */
export interface HealthResponse {
  status: "healthy" | "degraded" | "unhealthy";
  /** Components and their health, e.g. ``{"database": "healthy"}``. */
  components?: Record<string, string>;
  /** Optional human-readable detail. */
  detail?: string;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/**
 * Standard error envelope (see ``docs/API_SPEC.md`` §4). The UI
 * surfaces ``error.message`` in the toast and ``error.code`` as a
 * small monospace badge so a screenshot of the page is
 * self-explanatory.
 */
export interface ApiError {
  request_id: RequestId;
  status: "error";
  error: {
    code: string;
    message: string;
    /** Free-form structured details; shape varies by error code. */
    details?: Record<string, unknown>;
  };
}

/**
 * Thrown by the API client on a non-2xx response. The ``status``
 * field is the HTTP status code (so 429 / 401 / 500 are easy to
 * branch on); ``body`` is the parsed error envelope if it matched
 * the standard shape, otherwise the raw response text.
 */
export class ApiClientError extends Error {
  public readonly status: number;
  public readonly body: ApiError | string;

  constructor(message: string, status: number, body: ApiError | string) {
    super(message);
    this.name = "ApiClientError";
    this.status = status;
    this.body = body;
  }

  /** True for HTTP 429 (rate limit). */
  isRateLimited(): boolean {
    return this.status === 429;
  }

  /** True for any 5xx. */
  isServerError(): boolean {
    return this.status >= 500;
  }

  /** True for the standard error envelope. */
  hasEnvelope(): boolean {
    return typeof this.body === "object" && this.body !== null;
  }
}
