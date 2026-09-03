import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useLandingState } from "./useLandingState";
import { askQuestion, createSession, getSession, isLiveMode } from "../lib/api";
import { getAuthSnapshot } from "../lib/authStore";
import { ApiClientError } from "../lib/types";
import type { AskResponse, CreateSessionResponse, GetSessionResponse } from "../lib/types";

// The hook talks to the backend through these functions; mock the whole
// module so no real network happens and ``isLiveMode`` is controllable per
// test. ``citationsToSources`` (a pure adapter) and ``useToast`` stay real
// so the wiring is exercised end-to-end.
vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => true),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
  getSession: vi.fn(),
}));

vi.mock("../lib/authStore", () => ({
  getAuthSnapshot: vi.fn(() => ({ status: "anonymous", user: null })),
}));

const mockIsLive = vi.mocked(isLiveMode);
const mockCreateSession = vi.mocked(createSession);
const mockAskQuestion = vi.mocked(askQuestion);
const mockGetSession = vi.mocked(getSession);
const mockGetAuthSnapshot = vi.mocked(getAuthSnapshot);

const session: CreateSessionResponse = {
  request_id: "req_1",
  session_id: "sess-1",
  expires_at: "2026-07-11T12:00:00Z",
};

function askResponse(over: Partial<AskResponse> = {}): AskResponse {
  return {
    request_id: "req_2",
    message_id: "msg_1",
    answer: "Live answer.",
    citations: [
      {
        source_name: "Claude Code Docs",
        title: "Permissions",
        url: "https://x",
        chunk_id: "c1",
        marker: 1,
      },
    ],
    domain: "claude_code",
    intent: "how_to",
    confidence: "high",
    cache_hit: false,
    retrieval_strategy: "hybrid_reranked",
    unsupported: false,
    no_answer: false,
    source_version_hash: "hash",
    answer_policy_version: "v1",
    ...over,
  };
}

/** Advance enough fake time for the async fetch + word-streaming to settle. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  // Full reset (not just clear) so a `*Once` queued by one test cannot
  // bleed into the next when a call it expected never fires.
  mockIsLive.mockReset();
  mockCreateSession.mockReset();
  mockAskQuestion.mockReset();
  mockGetSession.mockReset();
  mockIsLive.mockReturnValue(true);
  mockCreateSession.mockResolvedValue(session);
  mockAskQuestion.mockResolvedValue(askResponse());
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("useLandingState — live send path", () => {
  it("creates a session, asks the backend, and streams the real answer + citations", async () => {
    const { result } = renderHook(() => useLandingState());

    act(() => {
      result.current.send("How do permissions work?");
    });
    await settle();

    const messages = result.current.state.messages;
    expect(messages[0]).toMatchObject({ role: "user", text: "How do permissions work?" });

    const bot = messages[1];
    expect(bot.role).toBe("bot");
    expect(bot.streaming).toBe(false);
    expect(bot.text).toBe("Live answer.");
    expect(bot.sources).toEqual([{ n: "1", title: "Permissions", url: "https://x" }]);

    expect(mockCreateSession).toHaveBeenCalledTimes(1);
    expect(mockAskQuestion).toHaveBeenCalledWith("sess-1", "How do permissions work?");
  });

  it("reuses one session across multiple questions", async () => {
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("First question?"));
    await settle();
    act(() => result.current.send("Second, different question?"));
    await settle();

    expect(mockCreateSession).toHaveBeenCalledTimes(1);
    expect(mockAskQuestion).toHaveBeenCalledTimes(2);
  });

  it("marks an unsupported response as a refusal", async () => {
    mockAskQuestion.mockResolvedValue(
      askResponse({ answer: "Out of scope.", unsupported: true, no_answer: true, citations: [] }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("What laptop should I buy?"));
    await settle();

    const bot = result.current.state.messages[1];
    expect(bot.refusal).toBe(true);
    expect(bot.sources).toEqual([]);
    expect(bot.suggestions ?? []).toEqual([]);
  });

  it("threads graceful-fallback nearest-doc suggestions onto a no_answer message", async () => {
    // A no_answer that retrieved evidence carries suggestions the UI should surface
    // instead of a bare refusal (Phase 4a).
    mockAskQuestion.mockResolvedValue(
      askResponse({
        answer: "No grounded answer.",
        unsupported: false,
        no_answer: true,
        citations: [],
        suggestions: [
          { title: "Claude Code Reference", url: "/claude-code", product_area: "claude_code" },
        ],
      }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("something the docs almost cover"));
    await settle();

    const bot = result.current.state.messages[1];
    expect(bot.refusal).toBe(true);
    expect(bot.suggestions).toEqual([
      { title: "Claude Code Reference", url: "/claude-code", product_area: "claude_code" },
    ]);
  });

  it("marks a grounded no-answer (no_answer only, unsupported false) as a refusal", async () => {
    // Backend emits unsupported:false, no_answer:true when the domain is
    // supported but the docs don't ground an answer — this must still
    // render as a refusal, exercising the `|| resp.no_answer` clause.
    mockAskQuestion.mockResolvedValue(
      askResponse({ answer: "No grounded answer.", unsupported: false, no_answer: true, citations: [] }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("An in-domain but unanswerable question"));
    await settle();

    expect(result.current.state.messages[1].refusal).toBe(true);
  });

  it("routes a live CiteVyn-meta question to the backend, not the client short-circuit (#49)", async () => {
    // The whole point of #49's frontend change: in live mode a question about
    // CiteVyn itself must reach the backend (which now indexes an About-CiteVyn
    // source) instead of the local matchCitevynMeta copy. This guards against a
    // regression that re-adds the short-circuit before the `if (live)` branch.
    mockAskQuestion.mockResolvedValue(
      askResponse({
        answer: "CiteVyn Pro is not live yet.",
        domain: "citevyn",
        citations: [
          {
            source_name: "About CiteVyn",
            title: "About CiteVyn",
            url: "/about",
            chunk_id: "cv1",
            marker: 1,
          },
        ],
      }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("What do I get with CiteVyn Pro?"));
    await settle();

    expect(mockAskQuestion).toHaveBeenCalledWith("sess-1", "What do I get with CiteVyn Pro?");
    const bot = result.current.state.messages[1];
    expect(bot.text).toBe("CiteVyn Pro is not live yet.");
    expect(bot.sources).toEqual([{ n: "1", title: "About CiteVyn", url: "/about" }]);
  });

  it("answers a CiteVyn-meta question from built-in copy in demo mode without hitting the backend", async () => {
    // Demo/offline fallback: no backend, so matchCitevynMeta still answers.
    mockIsLive.mockReturnValue(false);
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("What do I get with CiteVyn Pro?"));
    await settle();

    expect(mockAskQuestion).not.toHaveBeenCalled();
    const bot = result.current.state.messages[1];
    expect(bot.role).toBe("bot");
    expect(bot.refusal).toBe(false);
    // Assert on copy only matchCitevynMeta emits (not the word "pro", which is
    // already in the question) so this proves the built-in CiteVyn copy was returned.
    expect(bot.text.toLowerCase()).toContain("free to try");
  });

  it("de-dupes session creation for two concurrent asks", async () => {
    const { result } = renderHook(() => useLandingState());

    // Both fire before any await settles, so they share one in-flight
    // createSession promise (the sessionPromiseRef de-dupe).
    act(() => {
      result.current.send("First concurrent question");
      result.current.send("Second concurrent question");
    });
    await settle();

    expect(mockCreateSession).toHaveBeenCalledTimes(1);
    expect(mockAskQuestion).toHaveBeenCalledTimes(2);
  });

  it("does not bleed text between two concurrent live answers (stable-id targeting)", async () => {
    // Distinct answers whose word-streams overlap. Before stable-id targeting,
    // UPDATE_LAST_MESSAGE wrote every chunk into the tail bubble, so the second
    // answer's stream bled into the first's bubble and left a stuck cursor.
    mockAskQuestion
      .mockResolvedValueOnce(askResponse({ answer: "Answer ONE here.", citations: [] }))
      .mockResolvedValueOnce(askResponse({ answer: "Answer TWO here.", citations: [] }));
    const { result } = renderHook(() => useLandingState());

    act(() => {
      result.current.send("First question");
      result.current.send("Second question");
    });
    await settle();

    const bots = result.current.state.messages.filter((m) => m.role === "bot");
    expect(bots).toHaveLength(2);
    // Each bubble carries exactly one full answer — no interleaving/bleed.
    expect(bots.map((b) => b.text).sort()).toEqual(["Answer ONE here.", "Answer TWO here."]);
    // No bubble is left mid-stream with a blinking cursor.
    expect(bots.every((b) => b.streaming === false)).toBe(true);
    // Every message has a unique stable id.
    const ids = result.current.state.messages.map((m) => m.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe("useLandingState — live error path", () => {
  it("surfaces a rate-limit as a toast plus a rate-limit notice (NOT a content refusal)", async () => {
    mockAskQuestion.mockRejectedValue(
      new ApiClientError("Slow down.", 429, {
        request_id: "r",
        status: "error",
        error: { code: "rate_limited", message: "Slow down." },
      }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("A question that will be throttled"));
    await settle();

    const bot = result.current.state.messages[1];
    expect(bot.role).toBe("bot");
    // A 429 is a TRANSPORT failure, NOT a "NO SOURCE — REFUSED" content refusal (#120):
    // it carries the rate-limit errorKind and is not tagged as a refusal.
    expect(bot.errorKind).toBe("rate_limit");
    expect(bot.refusal).toBe(false);
    // The copy is deliberately non-technical (no "rate limit" jargon) — it tells the
    // user to slow down and retry, which is the recoverable action for a 429.
    expect(bot.text.toLowerCase()).toContain("too quickly");

    expect(result.current.toasts).toHaveLength(1);
    // A rate limit gets a DISTINCT, less-alarming visual: the amber "warning"
    // toast, not the red "error" alert used for server/transport failures.
    expect(result.current.toasts[0]).toMatchObject({
      kind: "warning",
      title: "Too many requests",
    });
  });

  it("appends the sign-in upsell to a rate-limit message for an anonymous visitor (ADR-0004 PR 11)", async () => {
    mockGetAuthSnapshot.mockReturnValue({ status: "anonymous", user: null });
    mockAskQuestion.mockRejectedValue(
      new ApiClientError("Slow down.", 429, {
        request_id: "r",
        status: "error",
        error: { code: "rate_limited", message: "Slow down." },
      }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("A question that will be throttled"));
    await settle();

    expect(result.current.state.messages[1].text).toContain("Sign in for a higher limit.");
  });

  it("does not pitch sign-in to an already-signed-in caller on a rate limit (ADR-0004 PR 11)", async () => {
    mockGetAuthSnapshot.mockReturnValue({
      status: "signed-in",
      user: { request_id: "r", user_id: "usr_1", email: "a@example.com", anonymous: false, providers: [], has_password: true, password_step_up: false },
    });
    mockAskQuestion.mockRejectedValue(
      new ApiClientError("Slow down.", 429, {
        request_id: "r",
        status: "error",
        error: { code: "rate_limited", message: "Slow down." },
      }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("A question that will be throttled"));
    await settle();

    expect(result.current.state.messages[1].text).not.toContain("Sign in for a higher limit.");
  });

  it("gives a limiter outage its own copy — not answer-service copy, not a refusal (#167)", async () => {
    // A Redis outage makes the server reject fail-closed with 503
    // ``rate_limiter_unavailable``. The old code (``index_unavailable``) fell
    // through to the generic 5xx branch and told the user the ANSWER SERVICE
    // was unreachable, which is a different (and wrong) fault.
    //
    // The envelope literal below is hand-built because ``askQuestion`` is
    // mocked at the hook boundary. That it MATCHES the real wire body is
    // pinned separately by ``api.test.ts`` ("errorCode() over the REAL wire
    // body"), which drives ``apiFetch`` with the body captured from the
    // backend — without that pairing this test proves nothing about
    // production.
    mockAskQuestion.mockRejectedValue(
      new ApiClientError("Rate limiter is temporarily unavailable.", 503, {
        request_id: "r",
        status: "error",
        error: { code: "rate_limiter_unavailable", message: "Rate limiter is temporarily unavailable." },
      }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("A question during a Redis outage"));
    await settle();

    const bot = result.current.state.messages[1];
    // Transport failure, so the "TEMPORARILY UNAVAILABLE" notice — never the
    // "NO SOURCE — REFUSED" content-refusal badge (#120/#142).
    expect(bot.errorKind).toBe("error");
    expect(bot.refusal).toBe(false);
    // Does not blame the answer service, and does not tell the user to slow down.
    expect(bot.text).toContain("temporarily unable to accept requests");
    expect(bot.text.toLowerCase()).not.toContain("answer service");
    expect(bot.text.toLowerCase()).not.toContain("too quickly");
    // No Redis/limiter internals leak into user-facing copy.
    expect(bot.text.toLowerCase()).not.toContain("redis");
    expect(result.current.toasts[0]).toMatchObject({
      kind: "error",
      title: "We can't take requests right now",
    });
  });

  it("still uses answer-service copy for an unrelated 503 (#167 regression guard)", async () => {
    // Edge case: a 503 WITHOUT the limiter code must keep the old copy — the
    // new branch must not swallow every 503.
    mockAskQuestion.mockRejectedValue(
      new ApiClientError("boom", 503, {
        request_id: "r",
        status: "error",
        error: { code: "index_unavailable", message: "boom" },
      }),
    );
    const { result } = renderHook(() => useLandingState());
    act(() => result.current.send("A question during an index outage"));
    await settle();
    expect(result.current.state.messages[1].text).toContain("reaching the answer service");
  });

  it("uses a distinct toast kind for a rate limit vs a server error", async () => {
    // Rate limit → warning (transient, recoverable).
    mockAskQuestion.mockRejectedValueOnce(new ApiClientError("Slow down.", 429, "Slow down."));
    const { result } = renderHook(() => useLandingState());
    act(() => result.current.send("throttled question"));
    await settle();
    expect(result.current.toasts[0].kind).toBe("warning");

    // Server error → error (a genuine failure of the service).
    mockAskQuestion.mockRejectedValueOnce(new ApiClientError("boom", 503, "boom"));
    act(() => result.current.send("another question after 5xx"));
    await settle();
    expect(result.current.toasts[result.current.toasts.length - 1].kind).toBe("error");
  });

  it("allows re-asking the same question after a live error (retry is not dropped)", async () => {
    mockAskQuestion
      .mockRejectedValueOnce(
        new ApiClientError("boom", 503, {
          request_id: "r",
          status: "error",
          error: { code: "internal_error", message: "boom" },
        }),
      )
      .mockResolvedValueOnce(askResponse({ answer: "Recovered answer." }));
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("How do permissions work?"));
    await settle();
    act(() => result.current.send("How do permissions work?"));
    await settle();

    // The retry must actually hit the backend again, not be swallowed by
    // the duplicate-question guard.
    expect(mockAskQuestion).toHaveBeenCalledTimes(2);
    expect(result.current.state.messages.at(-1)?.text).toBe("Recovered answer.");
    // #121: the retry re-shows the user's question — there are TWO user bubbles for
    // it, so the recovered answer is not an orphaned bot bubble with no question above.
    const userAsks = result.current.state.messages.filter(
      (m) => m.role === "user" && m.text === "How do permissions work?",
    );
    expect(userAsks).toHaveLength(2);
  });

  it("re-creates the session after a 404 so an expired session recovers", async () => {
    mockAskQuestion
      .mockRejectedValueOnce(
        new ApiClientError("Session not found", 404, {
          request_id: "r",
          status: "error",
          error: { code: "not_found", message: "Session not found" },
        }),
      )
      .mockResolvedValueOnce(askResponse({ answer: "After re-create." }));
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("A question"));
    await settle();
    expect(mockCreateSession).toHaveBeenCalledTimes(1);

    act(() => result.current.send("A question"));
    await settle();

    // The dead session id must be dropped and a fresh session created.
    expect(mockCreateSession).toHaveBeenCalledTimes(2);
    expect(result.current.state.messages.at(-1)?.text).toBe("After re-create.");
  });

  it("labels a 5xx with a plain, customer-facing service-unavailable message", async () => {
    mockAskQuestion.mockRejectedValue(
      new ApiClientError("boom", 503, {
        request_id: "r",
        status: "error",
        error: { code: "internal_error", message: "boom" },
      }),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("Another question"));
    await settle();

    expect(result.current.toasts[0]).toMatchObject({ kind: "error", title: "We couldn't get an answer" });
    // The copy must stay non-technical and offer the honest escalation for a
    // persistent outage (e.g. a provider usage limit) rather than leaking internals.
    expect(result.current.toasts[0].message.toLowerCase()).toContain("contact support");
  });

  it("shows a generic error for a status-0 network/timeout failure", async () => {
    // apiFetch wraps network + timeout errors as a status-0 ApiClientError,
    // which falls to the generic branch of handleApiError.
    mockAskQuestion.mockRejectedValue(
      new ApiClientError("Network error — is the backend running?", 0, "Network error"),
    );
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("Ask while backend is down"));
    await settle();

    expect(result.current.toasts[0]).toMatchObject({ kind: "error", title: "Something went wrong" });
    // A network/timeout failure is a transport error, NOT a content refusal (#120).
    expect(result.current.state.messages[1].errorKind).toBe("error");
    expect(result.current.state.messages[1].refusal).toBe(false);
  });

  it("retries session creation after it fails once, then succeeds", async () => {
    mockCreateSession
      .mockRejectedValueOnce(
        new ApiClientError("db down", 503, {
          request_id: "r",
          status: "error",
          error: { code: "internal_error", message: "db down" },
        }),
      )
      .mockResolvedValueOnce(session);
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("A question"));
    await settle();
    // First attempt: session creation failed → error surfaced, no ask made.
    expect(result.current.toasts).toHaveLength(1);
    expect(mockAskQuestion).not.toHaveBeenCalled();

    // Retry the same question: promise cache was cleared, so createSession
    // is invoked again and the ask now succeeds.
    act(() => result.current.send("A question"));
    await settle();

    expect(mockCreateSession).toHaveBeenCalledTimes(2);
    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
    expect(result.current.state.messages.at(-1)?.text).toBe("Live answer.");
  });
});

describe("useLandingState — demo fallback", () => {
  it("uses the canned KB and never touches the backend when live is off", async () => {
    mockIsLive.mockReturnValue(false);
    const { result } = renderHook(() => useLandingState());

    act(() => result.current.send("How do I use Claude Code?"));
    await settle();

    expect(mockCreateSession).not.toHaveBeenCalled();
    expect(mockAskQuestion).not.toHaveBeenCalled();

    const bot = result.current.state.messages[1];
    expect(bot.role).toBe("bot");
    expect(bot.text.length).toBeGreaterThan(0);
  });

  it("reports live=false to consumers in demo mode", () => {
    mockIsLive.mockReturnValue(false);
    const { result } = renderHook(() => useLandingState());
    expect(result.current.live).toBe(false);
  });
});

describe("useLandingState — resumeSession (ADR-0004 PR 10)", () => {
  function getSessionResponse(over: Partial<GetSessionResponse> = {}): GetSessionResponse {
    return {
      request_id: "req_3",
      session_id: "sess-old",
      user_id: "usr_a",
      channel: "chat",
      summary: null,
      current_product_area: "claude_code",
      created_at: "2026-09-01T00:00:00Z",
      expires_at: "2026-09-08T00:00:00Z",
      messages: [
        {
          message_id: "m1",
          role: "user",
          content: "What is Claude Code?",
          normalized_query: null,
          domain: null,
          intent: null,
          created_at: null,
          citations: [],
        },
        {
          message_id: "m2",
          role: "assistant",
          content: "Claude Code is a CLI tool.",
          normalized_query: null,
          domain: "claude_code",
          intent: "faq",
          created_at: null,
          citations: [
            { source_name: "docs", title: "Overview", url: "https://x", chunk_id: "c1", marker: 1 },
          ],
        },
      ],
      ...over,
    };
  }

  it("replaces the transcript wholesale and switches to the chat screen", async () => {
    mockGetSession.mockResolvedValue(getSessionResponse());
    const { result } = renderHook(() => useLandingState());

    act(() => {
      result.current.dispatch({
        type: "ADD_MESSAGE",
        message: { id: 999, role: "user", text: "stale message that must be replaced" },
      });
    });
    expect(result.current.state.messages).toHaveLength(1);

    await act(async () => {
      await result.current.resumeSession("sess-old");
    });

    expect(result.current.state.messages).toHaveLength(2);
    expect(result.current.state.messages[0]).toMatchObject({ role: "user", text: "What is Claude Code?" });
    expect(result.current.state.messages[1]).toMatchObject({
      role: "bot",
      text: "Claude Code is a CLI tool.",
    });
    expect(result.current.state.messages[1].sources?.[0]?.title).toBe("Overview");
    expect(result.current.screen).toBe("chat");
  });

  it("pins the resumed session id so the NEXT question continues it, not a fresh session", async () => {
    mockGetSession.mockResolvedValue(getSessionResponse({ session_id: "sess-continue" }));
    const { result } = renderHook(() => useLandingState());

    await act(async () => {
      await result.current.resumeSession("sess-continue");
    });
    act(() => result.current.send("a follow-up question"));
    await settle();

    // createSession must NOT have been called -- ensureSession() only
    // mints a new one when sessionIdRef.current is still null.
    expect(mockCreateSession).not.toHaveBeenCalled();
    expect(mockAskQuestion).toHaveBeenCalledWith("sess-continue", "a follow-up question");
  });

  it("a fetch failure surfaces a toast and does not change the current screen", async () => {
    mockGetSession.mockRejectedValue(new ApiClientError("boom", 500, "boom"));
    const { result } = renderHook(() => useLandingState());

    await act(async () => {
      await result.current.resumeSession("sess-broken");
    });

    expect(result.current.screen).toBe("landing");
    expect(result.current.toasts.length).toBeGreaterThan(0);
  });

  it("a slow resume that resolves AFTER a newer one does not overwrite the newer transcript (review-caught race)", async () => {
    let resolveFirst!: (v: ReturnType<typeof getSessionResponse>) => void;
    const firstPromise = new Promise<ReturnType<typeof getSessionResponse>>((resolve) => {
      resolveFirst = resolve;
    });
    mockGetSession.mockReturnValueOnce(firstPromise);
    const { result } = renderHook(() => useLandingState());

    const firstCall = result.current.resumeSession("sess-old"); // does not resolve yet

    const secondResponse = getSessionResponse({
      session_id: "sess-new",
      messages: [
        {
          message_id: "n1",
          role: "user",
          content: "the newer conversation",
          normalized_query: null,
          domain: null,
          intent: null,
          created_at: null,
          citations: [],
        },
      ],
    });
    mockGetSession.mockResolvedValueOnce(secondResponse);
    await act(async () => {
      await result.current.resumeSession("sess-new");
    });
    expect(result.current.state.messages).toHaveLength(1);
    expect(result.current.state.messages[0].text).toBe("the newer conversation");

    // NOW the slow, superseded first call resolves -- it must be a no-op.
    await act(async () => {
      resolveFirst(getSessionResponse({ session_id: "sess-old" }));
      await firstCall;
    });

    expect(result.current.state.messages).toHaveLength(1);
    expect(result.current.state.messages[0].text).toBe("the newer conversation");
  });
});

// ---------------------------------------------------------------------------
// #302 — landing hands over to the chat. These live in vitest, not only in
// Playwright, because CI runs `npm test` but runs NO demo-mode Playwright job
// (see #311), so a Playwright-only guard on these behaviours is not enforced.
// ---------------------------------------------------------------------------
describe("useLandingState — landing hands over to the chat (#302)", () => {
  it("carries unsent hero text into the chat composer and clears the hero box", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.onHeroInput({
        target: { value: "draft I never sent" },
      } as React.ChangeEvent<HTMLInputElement>);
    });
    expect(result.current.state.heroInput).toBe("draft I never sent");

    // A landing entry point that is NOT the hero box (e.g. a persona question).
    act(() => {
      result.current.enterChat("What is Claude Code?");
    });
    expect(result.current.state.chatInput).toBe("draft I never sent");
    expect(result.current.state.heroInput).toBe("");
    await settle();
  });

  it("never carries over an existing chat draft", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.onChatInput({
        target: { value: "half-written chat question" },
      } as React.ChangeEvent<HTMLInputElement>);
      result.current.onHeroInput({
        target: { value: "hero text" },
      } as React.ChangeEvent<HTMLInputElement>);
    });
    act(() => {
      result.current.enterChat("What is Claude Code?");
    });
    // The draft survives, and the hero text is left where it was rather than
    // silently thrown away.
    expect(result.current.state.chatInput).toBe("half-written chat question");
    expect(result.current.state.heroInput).toBe("hero text");
    await settle();
  });

  it("a hero SUBMIT carries nothing across — its text became the question", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.onHeroInput({
        target: { value: "What is Claude Code?" },
      } as React.ChangeEvent<HTMLInputElement>);
    });
    act(() => {
      result.current.onAskHero();
    });
    await settle();
    // Partner assertion: the question really was asked, so an empty composer
    // here cannot be "nothing happened at all".
    expect(result.current.state.messages[0]).toMatchObject({
      role: "user",
      text: "What is Claude Code?",
    });
    expect(result.current.state.chatInput).toBe("");
  });

  it("re-asking an answered question highlights it via a -1 round trip, then clears", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await settle();
    expect(result.current.state.messages).toHaveLength(2);

    act(() => {
      result.current.send("what is CLAUDE code?"); // duplicate, case-insensitive
    });
    // No new bubble, and the highlight has NOT been applied yet: `flashExisting`
    // resets to -1 first so React re-renders and the CSS pulse replays even when
    // the same bubble is flashed twice in a row.
    expect(result.current.state.messages).toHaveLength(2);
    expect(result.current.state.highlight).toBe(-1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20);
    });
    expect(result.current.state.highlight).toBe(0);

    // ...and it clears itself, so a highlight can never be left stuck on.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(result.current.state.highlight).toBe(-1);
  });

  it("a second flash resets the highlight to -1 first, so the pulse can replay", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await settle();
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20);
    });
    expect(result.current.state.highlight).toBe(0); // pulsing

    // Re-ask the SAME question while it is still highlighted. Without the reset to
    // -1 the value never changes, React skips the re-render, and the CSS animation
    // does not replay — the user gets no feedback the second time.
    act(() => {
      result.current.send("What is Claude Code?");
    });
    expect(result.current.state.highlight).toBe(-1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20);
    });
    expect(result.current.state.highlight).toBe(0);
  });

  it("a superseded flash never applies its own index", async () => {
    // Each flash stops the previous flash's timers before arming its own. Without
    // that, an earlier flash's 10ms restart still fires and briefly highlights the
    // WRONG bubble on its way past.
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await settle();
    act(() => {
      result.current.send("How do I install the Codex CLI?");
    });
    await settle();
    expect(result.current.state.messages).toHaveLength(4);

    act(() => {
      result.current.send("What is Claude Code?"); // flash index 0, restart at +10
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5);
    });
    act(() => {
      result.current.send("How do I install the Codex CLI?"); // flash index 2, restart at +15
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(7); // t = 12: index 0's restart would have fired
    });
    expect(result.current.state.highlight).toBe(-1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10); // t = 22: index 2's restart has fired
    });
    expect(result.current.state.highlight).toBe(2);
  });

  it("unmounting between the reset and the restart leaves no timer to fire", async () => {
    // The 10ms restart timer used to be a bare `setTimeout`, invisible to the
    // unmount sweep. A post-unmount dispatch is a React state-update-on-unmounted
    // warning at best, and a leak either way.
    const errors: unknown[] = [];
    const spy = vi.spyOn(console, "error").mockImplementation((...a) => errors.push(a));
    const { result, unmount } = renderHook(() => useLandingState());
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await settle();
    act(() => {
      result.current.send("What is Claude Code?"); // duplicate -> arms both timers
    });
    // Partner assertion: the flash really did arm timers, so "none pending after
    // unmount" cannot pass because nothing was ever scheduled.
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    unmount();
    // The unmount sweep walks `timers.current`, so an UNTRACKED `setTimeout` would
    // still be sitting here.
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2500); // past BOTH the 10ms and the 2s timers
    });
    expect(errors).toEqual([]);
    spy.mockRestore();
  });
});
