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
  AuthCredentials,
  AuthUserResponse,
  CreateSessionRequest,
  CreateSessionResponse,
  ExactSearchRequest,
  ExactSearchResponse,
  GetSessionResponse,
  HealthResponse,
  ListMySessionsResponse,
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
export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

/**
 * Demo bearer token. Mirrors the backend's
 * ``CITEVYN_DEMO_API_KEY`` default (``local-demo-key``). V1 will
 * swap this for a real auth flow.
 */
const API_DEMO_KEY = import.meta.env.VITE_API_DEMO_KEY ?? "local-demo-key";

/** Default user id for session creation. */
const API_DEMO_USER_ID = import.meta.env.VITE_API_DEMO_USER_ID ?? "demo_user";

/**
 * Whether the chat should hit the real backend instead of the canned
 * ``knowledgeBase`` demo. Read at call time (not module load) so tests
 * can flip it with ``vi.stubEnv`` and so a future in-app toggle can
 * override it without a rebuild. Only the exact string ``"true"``
 * enables live mode; anything else (including unset) stays in demo.
 */
export function isLiveMode(): boolean {
  return import.meta.env.VITE_API_LIVE === "true";
}

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

// ---------------------------------------------------------------------------
// 401 interceptor (ADR-0004 PR 8)
// ---------------------------------------------------------------------------

const unauthorizedListeners = new Set<() => void>();

/**
 * Subscribe to every 401 response ``apiFetch`` sees, from any caller.
 * Returns an unsubscribe function. ``authStore`` is the only current
 * subscriber (it drops to signed-out state), but the hook lives here —
 * not in authStore — because it must fire for a 401 from ANY endpoint,
 * not only the auth routes authStore itself calls.
 */
export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

/**
 * ``fetch`` wrapper that handles auth, JSON, timeouts, and the
 * standard error envelope. Most modules should use the typed
 * helpers below; this is exported for edge cases (e.g. the V1
 * streaming endpoint that uses ``text/event-stream``).
 */
export async function apiFetch<T>(
  path: string,
  init: RequestInit = {},
  options: { timeoutMs?: number; signal?: AbortSignal; skipUnauthorizedInterceptor?: boolean } = {},
): Promise<T> {
  const {
    timeoutMs = DEFAULT_TIMEOUT_MS,
    signal: externalSignal,
    skipUnauthorizedInterceptor = false,
  } = options;

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
    // Required for the ADR-0004 session cookie to travel: fetch's default
    // credentials mode ("same-origin") already covers same-origin deploys
    // (prod's StaticFiles mount, Vite's dev proxy), but "include" is what
    // the ADR specifies and it degrades gracefully to the same behavior —
    // explicit rather than relying on a default that a future cross-origin
    // deploy (a separately-hosted frontend pointed at VITE_API_BASE_URL)
    // would silently break.
    response = await fetch(url, {
      ...init,
      headers,
      signal: controller.signal,
      credentials: "include",
    });
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
    if (response.status === 401 && !skipUnauthorizedInterceptor) {
      // ADR-0004 PR 8's "401 interceptor": notify whoever is listening
      // (authStore) that the caller's session is no longer valid, so the
      // UI drops to signed-out state even when the 401 arrived from a
      // call authStore didn't itself make (e.g. a stale AccountMenu action
      // that raced a session expiry). A no-op until something subscribes.
      //
      // getCurrentUser() opts OUT via skipUnauthorizedInterceptor: a 401
      // from GET /v1/auth/me is not "the session expired mid-action", it
      // is the NORMAL shape of "not authenticated", and authStore's own
      // bootstrapAuth() already interprets it correctly — including
      // discarding a stale response race-caught in review (a slow /me
      // 401 landing after a fast login() already set signed-in). Without
      // this the global interceptor would apply its own unconditional
      // downgrade before bootstrapAuth's request-ordering guard ever gets
      // a chance to run.
      for (const listener of unauthorizedListeners) listener();
    }
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
  // The backend's ``ExactSearchRequest`` names this field ``limit``
  // (see ``backend/app/api/routes/search.py``). The client contract
  // in ``types.ts`` exposes it as ``max_results`` for readability, so
  // we translate at the wire boundary here — sending ``max_results``
  // would be silently ignored and the server would always cap at 10.
  return apiFetch<ExactSearchResponse>("/v1/search/exact", {
    method: "POST",
    body: JSON.stringify({
      product_area: body.product_area,
      term: body.term,
      limit: body.max_results ?? 10,
    }),
  });
}

/** Liveness probe — used by the About view. */
export async function getHealth(): Promise<HealthResponse> {
  return apiFetch<HealthResponse>("/health");
}

// ---------------------------------------------------------------------------
// Auth (ADR-0004 PR 8)
// ---------------------------------------------------------------------------

export async function register(credentials: AuthCredentials): Promise<AuthUserResponse> {
  return apiFetch<AuthUserResponse>("/v1/auth/register", {
    method: "POST",
    body: JSON.stringify(credentials),
  });
}

export async function login(credentials: AuthCredentials): Promise<AuthUserResponse> {
  return apiFetch<AuthUserResponse>("/v1/auth/login", {
    method: "POST",
    body: JSON.stringify(credentials),
  });
}

export async function logout(): Promise<void> {
  await apiFetch<null>("/v1/auth/logout", { method: "POST" });
}

// ADR-0004 PR 14's magic-link / password calls live in ``lib/authActions.ts``,
// imported only by the lazy AuthModal, so they ride its chunk (bundle budget).

/**
 * Current identity, or ``null`` if there is no valid session — a 401 here
 * is the expected "not signed in" shape, not an error to surface, so it is
 * caught and normalized rather than left for the caller to special-case.
 */
export async function getCurrentUser(): Promise<AuthUserResponse | null> {
  try {
    return await apiFetch<AuthUserResponse>(
      "/v1/auth/me",
      {},
      { skipUnauthorizedInterceptor: true },
    );
  } catch (err) {
    if (err instanceof ApiClientError && err.status === 401) return null;
    throw err;
  }
}

// ---------------------------------------------------------------------------
// Session history (ADR-0004 PR 10)
// ---------------------------------------------------------------------------

/** The caller's own sessions, newest first. Works for an anonymous visitor too. */
export async function listMySessions(): Promise<ListMySessionsResponse> {
  return apiFetch<ListMySessionsResponse>("/v1/me/sessions");
}

/** Fetch a session with its messages (and their citations) for resume. */
export async function getSession(sessionId: string): Promise<GetSessionResponse> {
  return apiFetch<GetSessionResponse>(`/v1/sessions/${sessionId}`);
}
