/**
 * HTTP client for the CiteVyn backend.
 *
 * Single ``apiFetch`` wrapper that:
 *   - prefixes requests with the configured API base URL,
 *   - attaches the demo bearer token,
 *   - parses JSON on success,
 *   - parses the standard error envelope on failure and throws
 *     :class:`ApiClientError` (see ``./types.ts``).
 *
 * Every other module calls :func:`createSession`, :func:`askQuestion`,
 * :func:`exactSearch`, :func:`getHealth` — they are the only HTTP
 * surfaces the UI exercises in this slice.
 */

import type {
  AskResponse,
  CreateSessionRequest,
  CreateSessionResponse,
  ExactSearchRequest,
  ExactSearchResponse,
  HealthResponse,
} from "./types";
import { ApiClientError } from "./types";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/**
 * Base URL of the backend.
 *
 * Resolution order:
 *   1. ``VITE_API_BASE_URL`` (set in ``.env.local`` for production
 *      builds pointing at a deployed backend).
 *   2. The empty string, which makes :func:`apiFetch` use relative
 *      paths. Vite's dev server proxies ``/v1`` and ``/health`` to
 *      ``http://127.0.0.1:8000`` (see ``vite.config.ts``), so this
 *      "just works" during local development.
 */
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

/**
 * Demo bearer token. Mirrors the backend's
 * ``CITEVYN_DEMO_API_KEY`` default (``local-demo-key``). V1 will
 * swap this for a real auth flow.
 */
const API_DEMO_KEY = import.meta.env.VITE_API_DEMO_KEY ?? "local-demo-key";

/** Default user id for session creation. */
const API_DEMO_USER_ID = import.meta.env.VITE_API_DEMO_USER_ID ?? "demo_user";

// ---------------------------------------------------------------------------
// Core fetch wrapper
// ---------------------------------------------------------------------------

/**
 * The default request timeout, in milliseconds. The backend's
 * /v1/sessions/{id}/messages endpoint does not have a strict SLA
 * but the orchestrator can take a few seconds on a cold cache;
 * 20s is the ceiling before the UI shows a retry button.
 */
const DEFAULT_TIMEOUT_MS = 20_000;

/**
 * ``fetch`` wrapper that handles auth, JSON, timeouts, and the
 * standard error envelope. Most modules should use the typed
 * helpers below; this is exported for edge cases (e.g. the V1
 * streaming endpoint that uses ``text/event-stream``).
 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  options: { timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal: externalSignal } = options;

  // Compose an AbortController that fires on either caller-cancel
  // or timeout. We intentionally do not cancel on success — the
  // caller's signal only matters during in-flight requests.
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  const onExternalAbort = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener("abort", onExternalAbort, { once: true });
    }
  }

  const url = `${API_BASE_URL}${path}`;
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  // Bearer token. The backend's auth dev-keys are documented in
  // ``docs/SECURITY_MODEL.md`` and configured via
  // ``CITEVYN_DEMO_API_KEY`` on the server.
  headers.set("Authorization", `Bearer ${API_DEMO_KEY}`);

  let response: Response;
  try {
    response = await fetch(url, { ...init, headers, signal: controller.signal });
  } catch (err) {
    window.clearTimeout(timeoutId);
    if (externalSignal) externalSignal.removeEventListener("abort", onExternalAbort);
    if (controller.signal.aborted) {
      throw new ApiClientError(
        "Request timed out — the server took too long to respond.",
        0,
        "Request timed out.",
      );
    }
    throw new ApiClientError(
      "Network error — is the backend running?",
      0,
      err instanceof Error ? err.message : String(err),
    );
  }

  window.clearTimeout(timeoutId);
  if (externalSignal) externalSignal.removeEventListener("abort", onExternalAbort);

  // Read the body once, regardless of status. 204 No Content is
  // returned by some admin routes; treat as null.
  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      // Not JSON — fall through with raw text.
      parsed = text;
    }
  }

  if (!response.ok) {
    const body =
      typeof parsed === "object" && parsed !== null
        ? (parsed as { request_id?: string; status?: string; error?: { code?: string; message?: string } })
        : text;
    const message =
      typeof body === "object" && body !== null && "error" in body && body.error
        ? (body.error.message ?? `Request failed with status ${response.status}.`)
        : `Request failed with status ${response.status}.`;
    throw new ApiClientError(message, response.status, body as never);
  }

  return parsed as T;
}

// ---------------------------------------------------------------------------
// Typed helpers
// ---------------------------------------------------------------------------

/**
 * Create a new session.
 *
 * The backend requires ``user_id``. The UI uses the default
 * ``demo_user`` so the per-user rate limit (30/hour) applies —
 * the reviewer is encouraged to hammer the demo and watch the
 * toast appear.
 */
export async function createSession(
  body: Partial<CreateSessionRequest> = {},
): Promise<CreateSessionResponse> {
  return apiFetch<CreateSessionResponse>("/v1/sessions", {
    method: "POST",
    body: JSON.stringify({
      user_id: body.user_id ?? API_DEMO_USER_ID,
      channel: body.channel ?? "chat",
    }),
  });
}

/** Ask a question in a session. */
export async function askQuestion(
  sessionId: string,
  message: string,
  answerStyle: "short" | "step_by_step" = "short",
): Promise<AskResponse> {
  return apiFetch<AskResponse>(`/v1/sessions/${sessionId}/messages`, {
    method: "POST",
    body: JSON.stringify({ message, answer_style: answerStyle }),
  });
}

/** Look up an exact term. */
export async function exactSearch(body: ExactSearchRequest): Promise<ExactSearchResponse> {
  return apiFetch<ExactSearchResponse>("/v1/search/exact", {
    method: "POST",
    body: JSON.stringify({
      product_area: body.product_area,
      term: body.term,
      max_results: body.max_results ?? 10,
    }),
  });
}

/** Liveness probe — used by the About view. */
export async function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}
