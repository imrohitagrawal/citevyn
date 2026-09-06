import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useLandingState } from "./useLandingState";
import { askQuestion, createSession, getSession, isLiveMode } from "../lib/api";
import { getAuthSnapshot } from "../lib/authStore";
import { KB } from "../data/knowledgeBase";
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

  it("a highlight never survives leaving the chat screen", async () => {
    // A stale highlight is an INDEX into a list the next mount may not share, and
    // ChatView scrolls to whatever it points at — so re-entering the chat within the
    // 2s window would open on an unrelated message instead of the newest one.
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await settle();
    act(() => {
      result.current.send("What is Claude Code?"); // duplicate -> flash
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20);
    });
    expect(result.current.state.highlight).toBe(0); // armed, so the clear is real

    act(() => {
      result.current.backToLanding();
    });
    expect(result.current.state.highlight).toBe(-1);
  });

  it("leaving BEFORE the 10ms restart fires cancels it rather than racing it", async () => {
    // The reducer clears `highlight` on SET_SCREEN, but that alone is not enough:
    // a flash arms a 10ms restart timer, and if it survives the screen change it
    // sets the highlight straight back on a screen that no longer has that message.
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await settle();
    act(() => {
      result.current.send("What is Claude Code?"); // flash: restart armed for +10ms
      result.current.backToLanding(); // ...and we leave immediately, before it fires
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(result.current.state.highlight).toBe(-1);
  });

  it("a nav link out of the chat cancels an in-flight flash too", async () => {
    // `goSection` is the OTHER way out of the chat screen, and unlike "Back to
    // landing" it does not clear the hero box — it is a genuinely separate path.
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.enterChat("What is Claude Code?"); // must be ON the chat screen
    });
    await settle();
    expect(result.current.state.screen).toBe("chat");
    act(() => {
      result.current.send("What is Claude Code?"); // flash armed
    });
    act(() => {
      result.current.goSection(
        { preventDefault() {} } as React.MouseEvent,
        "who",
      );
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(result.current.state.screen).toBe("landing");
    expect(result.current.state.highlight).toBe(-1);
  });

  it("a highlight never survives resuming a different session", async () => {
    mockGetSession.mockResolvedValue({
      request_id: "r",
      session_id: "old-1",
      messages: [
        { role: "user", content: "older question", citations: [] },
        { role: "assistant", content: "older answer", citations: [] },
      ],
    } as unknown as GetSessionResponse);
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
    expect(result.current.state.highlight).toBe(0);

    // Flash again and resume WITHOUT letting the 10ms restart fire first, so the
    // in-flight timer would re-apply a stale index to the replaced transcript.
    act(() => {
      result.current.send("What is Claude Code?");
    });
    await act(async () => {
      await result.current.resumeSession("old-1");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(result.current.state.messages).toHaveLength(2);
    expect(result.current.state.highlight).toBe(-1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(result.current.state.highlight).toBe(-1);
  });

  it("RESUME_SESSION itself invalidates the highlight, not just the screen change", async () => {
    // `resumeSession` also dispatches SET_SCREEN, which clears the highlight — so
    // the reducer case above would look guarded while doing nothing. Drive the
    // action on its own: `highlight` is an INDEX into the list being replaced, and
    // replacing the list is what makes it meaningless.
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
    expect(result.current.state.highlight).toBe(0);

    act(() => {
      result.current.dispatch({
        type: "RESUME_SESSION",
        messages: [{ id: 99, role: "user", text: "a different conversation" }],
      });
    });
    expect(result.current.state.highlight).toBe(-1);
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
    const before = vi.getTimerCount();
    act(() => {
      result.current.send("What is Claude Code?"); // duplicate -> arms both timers
    });
    // Partner assertion, as a DELTA. A bare `getTimerCount() > 0` proves nothing
    // here: the hook always has its placeholder interval and hero loop pending, so
    // that assertion holds even if the flash armed no timers at all.
    expect(vi.getTimerCount()).toBe(before + 2); // the 10ms restart + the 2s clear
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

describe("useLandingState — the landing hero does not run on the chat screen (#312)", () => {
  it("keeps the chatView array IDENTITY stable when only hero state changes", async () => {
    // This is the defect, encoded directly. `chatView` used to be a bare `.map`, so
    // every SET_HERO dispatch -- ~40 per second from the landing demo animation --
    // handed `ChatView` a brand new `messages` array. Its passive `[messages]` effect
    // then ran and wrote `scrollTop`: measured 197 writes in 12 IDLE seconds with no
    // user interaction, which is the 42Hz scroll fight that made #302 reproduce only
    // in production while the entire suite stayed green.
    const { result } = renderHook(() => useLandingState());
    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    await settle();

    const before = result.current.chatView;
    // Drive hero state directly, so this measures the memo and not the screen gate.
    await act(async () => {
      result.current.dispatch({ type: "SET_HERO", hero: { text: "a" } });
      result.current.dispatch({ type: "SET_HERO", hero: { text: "ab" } });
      result.current.dispatch({ type: "SET_HERO", hero: { text: "abc" } });
    });

    expect(result.current.chatView).toBe(before);
  });

  it("still produces a NEW chatView when the messages actually change", async () => {
    // Partner: without this, `toBe(before)` above would also pass if `chatView` were
    // frozen, memoised on `[]`, or never rebuilt at all.
    const { result } = renderHook(() => useLandingState());
    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    await settle();
    const before = result.current.chatView;

    await act(async () => {
      result.current.send("How do I install the Codex CLI?");
    });
    await settle();

    expect(result.current.chatView).not.toBe(before);
    expect(result.current.chatView.length).toBeGreaterThan(before.length);
  });

  it("rebuilds chatView when the HIGHLIGHT changes, not only the messages", async () => {
    // `state.highlight` drives the duplicate-question pulse styling inside the map,
    // so it must be a dependency. Memoising on `[state.messages]` alone type-checks,
    // passes every other test here, and silently kills the #302 flash -- the whole
    // reason ChatView gets a highlight at all.
    const { result } = renderHook(() => useLandingState());
    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    await settle();
    const before = result.current.chatView;
    expect(before[0].userStyle.animation).toBeUndefined(); // partner: no pulse yet

    await act(async () => {
      result.current.dispatch({ type: "SET_HIGHLIGHT", index: 0 });
    });

    expect(result.current.chatView).not.toBe(before);
    expect(result.current.chatView[0].userStyle.animation).toContain("cv-pulse");
  });

  it("freezes the hero mid-stream when the chat screen takes over", async () => {
    // Entering chat WHILE a hero answer is still streaming is the case that needs
    // `timers.current.heroLoop?.stop()`. Leaving between items is covered by the
    // heroPause test below; without this one, dropping the heroLoop stop passes.
    const { result } = renderHook(() => useLandingState());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300); // mid-stream, not at a pause
    });
    expect(result.current.state.hero.streaming).toBe(true); // partner: really streaming

    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    // Capture IMMEDIATELY -- no `settle()`. Settling advances 4000ms, which is long
    // enough for the hero stream to finish on its own, and then a frozen-vs-frozen
    // comparison holds whether or not anything stopped it. That is exactly how the
    // first version of this test passed with `heroLoop?.stop()` deleted.
    const frozen = result.current.state.hero.text;
    expect(result.current.state.hero.streaming).toBe(true); // partner: still mid-stream

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(result.current.state.hero.text).toBe(frozen);
  });

  it("restarts the hero from its FIRST item on returning to landing", async () => {
    // Pins what actually happens, because I claimed the wrong thing. `playHeroLoop`
    // re-initialises `let idx = 0` on every call, so returning to the landing page
    // restarts the reel rather than resuming it. My browser probe only checked that
    // the hero text CHANGED after coming back -- which a restart satisfies just as
    // well as a resume -- so the claim outran the evidence. Whichever behaviour is
    // wanted, it should be the one written down and guarded.
    const { result } = renderHook(() => useLandingState());
    const firstKey = result.current.state.hero.key;
    // Advance past the first item so the reel has genuinely moved on.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(14000);
    });
    const laterKey = result.current.state.hero.key;
    expect(laterKey).not.toBe(firstKey); // partner: the reel really does advance

    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    await settle();
    await act(async () => {
      result.current.backToLanding();
      await vi.advanceTimersByTimeAsync(50);
    });

    expect(result.current.state.hero.key).toBe(firstKey);
  });

  it("stops rotating the hero placeholder on the chat screen", async () => {
    const { result } = renderHook(() => useLandingState());
    const first = result.current.heroPlaceholder;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3400); // past the 3200ms interval
    });
    // Partner: it really does rotate on the landing screen.
    expect(result.current.heroPlaceholder).not.toBe(first);

    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    await settle();
    const onChat = result.current.heroPlaceholder;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(result.current.heroPlaceholder).toBe(onChat);
  });

  it("does not let the self-re-arming heroPause restart the loop after leaving", async () => {
    // `playHeroLoop` schedules the NEXT item through `timers.current.heroPause`
    // (a 4600ms timeout) once a hero answer finishes streaming. Stopping only
    // `heroLoop` on the way out therefore looks correct and is not: 4.6s later
    // that pause fires, dispatches SET_HERO and re-arms the whole loop from a
    // handle nobody is holding. Without this test, deleting
    // `timers.current.heroPause?.stop()` passes the entire suite.
    const { result } = renderHook(() => useLandingState());
    // Let a full hero item stream and finish, so `heroPause` is genuinely armed.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    await settle();

    const heroTextOnEntry = result.current.state.hero.text;
    const timersOnEntry = vi.getTimerCount();

    // Well past the 4600ms pause.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });

    expect(result.current.state.hero.text).toBe(heroTextOnEntry);
    expect(vi.getTimerCount()).toBeLessThanOrEqual(timersOnEntry);
  });

  it("stops the hero + placeholder timers on the chat screen and restarts them on return", async () => {
    // The loop re-arms itself through `timers.current.heroPause`, so stopping only
    // `heroLoop` would let it resume 4.6s later from a handle nobody is holding.
    const { result } = renderHook(() => useLandingState());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    const onLanding = vi.getTimerCount();
    expect(onLanding).toBeGreaterThan(0); // partner: the loop really is running here

    await act(async () => {
      result.current.enterChat("What is Claude Code?");
    });
    await settle();
    const onChat = vi.getTimerCount();

    await act(async () => {
      result.current.backToLanding();
      await vi.advanceTimersByTimeAsync(200);
    });
    const backOnLanding = vi.getTimerCount();

    expect(onChat).toBeLessThan(onLanding);
    expect(backOnLanding).toBeGreaterThan(onChat);
  });
});

/**
 * #331. The `/` shortcut is a `window` keydown listener, so the dialog focus
 * trap — which only handles Tab and Escape — never saw it. With a drawer open
 * it focused the hero input BEHIND the backdrop, and the keystrokes after it
 * were not merely lost: a reviewer typed a question, pressed Enter, and the app
 * navigated to the chat screen. That is the same hazard #331 is about
 * (operating a control inside a region `aria-modal="true"` calls inert),
 * reached by a different key.
 */
describe("the / shortcut respects an open modal dialog (#331)", () => {
  function mountHeroInput() {
    const input = document.createElement("input");
    input.id = "hero-input";
    document.body.appendChild(input);
    return input;
  }

  function mountDialog() {
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    document.body.appendChild(dialog);
    return dialog;
  }

  afterEach(() => {
    document.querySelectorAll("#hero-input, [role='dialog']").forEach((n) => n.remove());
  });

  // The partner: proves the shortcut genuinely works, so the assertion below
  // cannot pass because `/` was broken for some unrelated reason.
  it("still focuses the hero input when NO dialog is open", () => {
    renderHook(() => useLandingState());
    const input = mountHeroInput();
    document.body.focus();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(input);
  });

  it("does NOT reach past an open dialog to the hero input behind it", async () => {
    const { useFocusTrap } = await import("./useFocusTrap");
    renderHook(() => useLandingState());
    const input = mountHeroInput();
    const dialogEl = mountDialog();
    // Register a real trap for that dialog, the way a drawer does.
    const { unmount } = renderHook(() => useFocusTrap({ current: dialogEl }));

    document.body.focus();
    const event = new KeyboardEvent("keydown", { key: "/", bubbles: true, cancelable: true });
    window.dispatchEvent(event);

    expect(document.activeElement).not.toBe(input);
    expect(event.defaultPrevented).toBe(false);
    unmount();
  });

  it("works again once the dialog closes", async () => {
    const { useFocusTrap } = await import("./useFocusTrap");
    renderHook(() => useLandingState());
    const input = mountHeroInput();
    const dialogEl = mountDialog();
    const { unmount } = renderHook(() => useFocusTrap({ current: dialogEl }));
    unmount();

    document.body.focus();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "/", bubbles: true, cancelable: true }));
    expect(document.activeElement).toBe(input);
  });
});

/**
 * #62 — the composer is gated while a live answer is in flight.
 *
 * What the ISSUE says is already fixed and its named code is gone: it blames a
 * shared `timers.current.chatTimer` and an `UPDATE_LAST_MESSAGE` that targets
 * "whichever message is last". PR #88 replaced both with a `Set` of per-stream
 * handles and stable-id targeting, and the test above
 * ("does not bleed text between two concurrent live answers") pins that.
 *
 * The symptom that SURVIVED is about attribution, not bleed. A bot bubble is
 * appended when its network call RESOLVES, while the user bubble is appended on
 * submit — so two questions asked back to back produce
 * [Q1][Q2][A1][A2], and a reader attributes A1 to Q2. Gating the composer on
 * the in-flight request makes that ordering unreachable from the composer: Q2
 * cannot be submitted until A1's bubble exists, so the transcript is
 * [Q1][A1][Q2][A2] by construction.
 *
 * The guard is a REF, not `state.pending`. Two Enter keypresses inside one
 * React batch both read the same rendered `state`, so a state-based guard sees
 * `pending: 0` twice and lets both through. The ref is written synchronously
 * before `sendLive`'s first await.
 */
describe("useLandingState — composer gating while an answer is in flight (#62)", () => {
  function type(result: { current: ReturnType<typeof useLandingState> }, value: string) {
    act(() => {
      result.current.onChatInput({
        target: { value },
      } as React.ChangeEvent<HTMLInputElement>);
    });
  }

  it("refuses a second submit while the first is in flight, and keeps the typed text", async () => {
    let releaseFirst: (r: AskResponse) => void = () => {};
    mockAskQuestion.mockImplementationOnce(
      () => new Promise<AskResponse>((resolve) => { releaseFirst = resolve; }),
    );
    const { result } = renderHook(() => useLandingState());

    type(result, "First question");
    await act(async () => {
      result.current.submitChat();
      await vi.advanceTimersByTimeAsync(100);
    });
    // Partner: the request really is in flight, so the refusal below is not
    // passing because nothing was ever sent.
    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
    expect(result.current.state.pending).toBeTruthy();

    type(result, "Second question");
    await act(async () => {
      result.current.submitChat();
      // Advance PAST the awaited `ensureSession()` a second call would have to
      // clear before it reaches `askQuestion` — asserting the count on the same
      // tick would read 1 whether the gate exists or not.
      await vi.advanceTimersByTimeAsync(200);
    });

    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
    // The refusal must not EAT the question: a silently cleared composer is a
    // worse bug than the one being fixed.
    expect(result.current.state.chatInput).toBe("Second question");
    expect(result.current.state.messages.filter((m) => m.role === "user")).toHaveLength(1);

    // Once the answer lands the composer takes it — the gate is transient.
    mockAskQuestion.mockResolvedValue(askResponse({ answer: "Second answer." }));
    await act(async () => {
      releaseFirst(askResponse({ answer: "First answer." }));
      await vi.advanceTimersByTimeAsync(4000);
    });
    act(() => {
      result.current.submitChat();
    });
    await settle();
    expect(mockAskQuestion).toHaveBeenCalledTimes(2);

    // The attribution symptom: every answer sits directly under its question.
    expect(result.current.state.messages.map((m) => m.role)).toEqual([
      "user", "bot", "user", "bot",
    ]);
    expect(result.current.state.messages.map((m) => m.text)).toEqual([
      "First question", "First answer.", "Second question", "Second answer.",
    ]);
  });

  it("refuses two submits fired inside ONE React batch", async () => {
    // The same-tick race a `state.pending` guard cannot see: both calls read the
    // state rendered before either of them ran.
    mockAskQuestion.mockImplementation(
      () => new Promise<AskResponse>(() => {}),
    );
    const { result } = renderHook(() => useLandingState());

    type(result, "First question");
    await act(async () => {
      result.current.submitChat();
      result.current.submitChat();
      await vi.advanceTimersByTimeAsync(200);
    });

    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
  });

  it("re-opens the composer after a FAILED request, not just a successful one", async () => {
    mockAskQuestion
      .mockRejectedValueOnce(new ApiClientError("boom", 500, {
        request_id: "r",
        status: "error",
        error: { code: "internal_error", message: "boom" },
      }))
      .mockResolvedValueOnce(askResponse({ answer: "Second answer." }));
    const { result } = renderHook(() => useLandingState());

    type(result, "First question");
    await act(async () => {
      result.current.submitChat();
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(result.current.state.pending).toBeFalsy();

    type(result, "Second question");
    await act(async () => {
      result.current.submitChat();
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(mockAskQuestion).toHaveBeenCalledTimes(2);
  });
});

/**
 * The "Searching the docs…" indicator is a COUNT of in-flight requests, not a
 * boolean. As a boolean it was cleared by whichever stream produced the first
 * chunk, so with two questions overlapping — reachable through `send`, which
 * every landing entry point uses and which the composer gate does not cover —
 * the indicator vanished while a question was still unanswered.
 */
describe("useLandingState — the pending indicator counts in-flight requests", () => {
  it("stays up while a SECOND question is still unanswered", async () => {
    let releaseSecond: (r: AskResponse) => void = () => {};
    mockAskQuestion
      .mockResolvedValueOnce(askResponse({ answer: "Answer ONE." }))
      .mockImplementationOnce(
        () => new Promise<AskResponse>((resolve) => { releaseSecond = resolve; }),
      );
    const { result } = renderHook(() => useLandingState());

    act(() => {
      result.current.send("First question");
      result.current.send("Second question");
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000);
    });

    // Partner: the first answer really did land and finish streaming, which is
    // exactly the event that used to clear the indicator.
    const bots = result.current.state.messages.filter((m) => m.role === "bot");
    expect(bots.map((b) => b.text)).toEqual(["Answer ONE."]);
    expect(bots[0].streaming).toBe(false);

    expect(result.current.state.pending).toBeTruthy();

    await act(async () => {
      releaseSecond(askResponse({ answer: "Answer TWO." }));
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(result.current.state.pending).toBeFalsy();
  });

  it("clears once both requests settle, however they settle", async () => {
    mockAskQuestion
      .mockRejectedValueOnce(new ApiClientError("boom", 500, {
        request_id: "r",
        status: "error",
        error: { code: "internal_error", message: "boom" },
      }))
      .mockResolvedValueOnce(askResponse({ answer: "Answer TWO." }));
    const { result } = renderHook(() => useLandingState());

    act(() => {
      result.current.send("First question");
      result.current.send("Second question");
    });
    await settle();

    expect(result.current.state.pending).toBeFalsy();
  });
});

/**
 * The demo rail: two overlapping streams, and a stream left running off-screen.
 *
 * `selectDemo` overwrote `timers.current.demoTimer` without stopping the handle
 * it replaced — the one thing `flashExisting` and (since PR #88) the chat path
 * both do. Both streams then wrote the SAME `state.demo.text`, so the visible
 * answer flipped between two different answers under the new question's heading.
 *
 * The same handle was also missing from the screen-gated effect that #312 added
 * for the hero loop and the placeholder rotation (#329). Stopping it there is
 * only half a fix: the panel would come back frozen mid-word with a caret
 * blinking forever, because `landing-sections.tsx` renders the caret on
 * `demo.streaming` and the sources on `demo.done`. Leaving the landing screen
 * therefore SNAPS the answer complete rather than freezing or restarting it.
 */
describe("useLandingState — the demo rail stream (#329 + the overlap it sits on)", () => {
  const FIRST = "claude-code";
  const SECOND = "codex-flag";

  it("never flips the visible answer backwards when a second question is picked mid-stream", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.selectDemo(FIRST);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    // Partner: the first stream really is mid-flight when the second starts.
    expect(result.current.state.demo.streaming).toBe(true);
    expect(result.current.state.demo.text.length).toBeGreaterThan(0);
    expect(result.current.state.demo.text.length).toBeLessThan(KB[FIRST].a.length);

    // The detector's preconditions, asserted so a future edit fails loudly
    // instead of silently disarming this test.
    expect(300 % 24).toBe(12);
    expect(KB[FIRST].a.length).toBeGreaterThan(KB[SECOND].a.length);

    act(() => {
      result.current.selectDemo(SECOND);
    });
    // Sample every 12ms, not every 24ms. The two streams tick on the same 24ms
    // grid 12ms apart, so a 24ms window contains one tick of EACH and React
    // batches them into one render — the `backwards` check below then observes
    // only the second and goes blind. (At 24ms the test still FAILS, on the
    // prefix assertion rather than this one; an earlier version of this comment
    // claimed it passed, which was wrong.)
    //
    // Both facts the detector rests on are asserted above rather than left
    // implicit, because either can be edited away by a change that looks
    // unrelated: the warm-up must be out of phase with the tick grid, and the
    // FIRST answer must be longer than the SECOND so the wrong stream wins the
    // length race.
    //
    // 240 samples x 12ms = 2880ms, comfortably past the SECOND answer's
    // 102 x 24ms = 2448ms, so the final-sample assertion below is reachable.
    const samples: string[] = [];
    for (let i = 0; i < 240; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(12);
      });
      samples.push(result.current.state.demo.text);
    }

    const backwards = samples.filter((s, i) => i > 0 && s.length < samples[i - 1].length);
    expect(backwards).toEqual([]);
    // Partner for a count-of-zero: the samples really did stream something.
    expect(samples[samples.length - 1]).toBe(KB[SECOND].a);
    // Every frame is a prefix of the NEW answer — never a frame of the old one.
    expect(samples.every((s) => KB[SECOND].a.startsWith(s))).toBe(true);
  });

  it("snaps the demo answer complete when the chat screen takes over, rather than freezing it mid-word", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.selectDemo(SECOND);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(result.current.state.demo.streaming).toBe(true);
    expect(result.current.state.demo.text.length).toBeLessThan(KB[SECOND].a.length);

    await act(async () => {
      result.current.enterChat(null);
    });

    // No caret left blinking over a truncated answer.
    expect(result.current.state.demo.text).toBe(KB[SECOND].a);
    expect(result.current.state.demo.streaming).toBe(false);
    expect(result.current.state.demo.done).toBe(true);
  });

  it("leaves no demo stream running behind the chat screen", async () => {
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.selectDemo(SECOND);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    await act(async () => {
      result.current.enterChat(null);
    });
    const snapped = result.current.state.demo.text;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(48);
    });

    // A stream still ticking would overwrite the snapped text with its own
    // short prefix on its very next tick. This is what separates "the timer was
    // stopped" from "the text happened to end up complete".
    expect(snapped).toBe(KB[SECOND].a);
    expect(result.current.state.demo.text).toBe(snapped);
  });

  it("still streams a demo answer normally on the landing screen", async () => {
    // Partner: the two guards above must not be passing because the demo stops
    // streaming altogether.
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.selectDemo(SECOND);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    const partway = result.current.state.demo.text;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });

    expect(partway.length).toBeGreaterThan(0);
    expect(result.current.state.demo.text.length).toBeGreaterThan(partway.length);
  });
});

/**
 * The two halves of the in-flight signal — the ref the gate reads and the count
 * the loader renders — are written in one place. These pin the properties that
 * make that safe, and the one that a reset would break.
 */
describe("useLandingState — the in-flight count cannot drift", () => {
  it("self-heals a SET_PENDING dispatched from outside markInFlight", async () => {
    // The hook exports `dispatch`, so "markInFlight is the only writer" is a
    // convention, not an invariant. Mirroring the ref rather than accumulating
    // a delta means a stray write is CORRECTED by the next real transition
    // instead of drifting for the rest of the session.
    const { result } = renderHook(() => useLandingState());
    act(() => {
      result.current.dispatch({ type: "SET_PENDING", value: 7 });
    });
    // Partner: the stray write really did land, so the heal below is a heal
    // and not a reducer that ignores this action.
    expect(result.current.state.pending).toBe(7);

    await act(async () => {
      result.current.send("A question");
      await vi.advanceTimersByTimeAsync(4000);
    });
    expect(result.current.state.pending).toBe(0);
  });

  it("starts with a demo that is NOT streaming, which is what makes SNAP_DEMO safe on mount", async () => {
    // React 18 StrictMode runs the landing effect's cleanup once on mount. That
    // cleanup dispatches SNAP_DEMO, so if the initial demo were ever seeded
    // mid-stream (the hero IS seeded `streaming: true`) the rail would arrive
    // pre-snapped and never animate in dev. Nothing else pins this literal.
    const { result } = renderHook(() => useLandingState());
    expect(result.current.state.demo.streaming).toBe(false);
    expect(result.current.state.demo.done).toBe(true);
  });

  it("survives a trip out to the landing screen and back", async () => {
    // `backToLanding` used to reset the indicator. With the gate reading a ref
    // that a reset does not touch, that would leave the loader gone while the
    // gate was still closed — and the next question would vanish with nothing
    // on screen explaining why.
    mockAskQuestion.mockImplementation(() => new Promise<AskResponse>(() => {}));
    const { result } = renderHook(() => useLandingState());

    act(() => {
      result.current.onChatInput({
        target: { value: "First question" },
      } as React.ChangeEvent<HTMLInputElement>);
    });
    await act(async () => {
      result.current.submitChat();
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(result.current.state.pending).toBe(1);

    await act(async () => {
      result.current.backToLanding();
      await vi.advanceTimersByTimeAsync(200);
    });
    await act(async () => {
      result.current.enterChat(null);
      await vi.advanceTimersByTimeAsync(200);
    });

    // The loader still shows, because the request really is still in flight —
    // and it agrees with the gate, which is still closed.
    expect(result.current.state.pending).toBe(1);
    act(() => {
      result.current.onChatInput({
        target: { value: "Second question" },
      } as React.ChangeEvent<HTMLInputElement>);
      result.current.submitChat();
    });
    expect(result.current.state.chatInput).toBe("Second question");
    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
  });

  it("leaves the demo untouched when nothing was streaming", async () => {
    // SNAP_DEMO is a no-op on an already-finished answer, by IDENTITY — the
    // landing panel is memo-sensitive (#312), so returning a fresh `demo`
    // object on every screen change would re-render it for nothing.
    const { result } = renderHook(() => useLandingState());
    const before = result.current.state.demo;
    expect(before.streaming).toBe(false);

    await act(async () => {
      result.current.enterChat(null);
    });

    expect(result.current.state.demo).toBe(before);
  });
});

/**
 * #62's residual, closed. The composer gate only ever covered `submitChat`;
 * every LANDING entry point (hero box, hero chips, marquee pills, persona
 * buttons, "Get Pro") reaches `send` through `enterChat` and was ungated. Four
 * independent reviewers reproduced the same three-click path — ask, Back, ask
 * again — and measured the answers arriving as [Q1][Q2][A2][A1], i.e. the
 * reader credits the SECOND answer to the FIRST question.
 */
describe("useLandingState — a landing entry point parks its question rather than racing one in flight (#62)", () => {
  async function askAndLeave(result: { current: ReturnType<typeof useLandingState> }) {
    act(() => {
      result.current.onChatInput({
        target: { value: "First question" },
      } as React.ChangeEvent<HTMLInputElement>);
    });
    await act(async () => {
      result.current.submitChat();
      await vi.advanceTimersByTimeAsync(200);
    });
    await act(async () => {
      result.current.backToLanding();
      await vi.advanceTimersByTimeAsync(200);
    });
  }

  it("parks the question in the composer instead of sending a second request", async () => {
    mockAskQuestion.mockImplementation(() => new Promise<AskResponse>(() => {}));
    const { result } = renderHook(() => useLandingState());
    await askAndLeave(result);
    // Partner: the first request really is still open, or there would be
    // nothing to park behind.
    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
    expect(result.current.state.pending).toBe(1);

    await act(async () => {
      result.current.enterChat("Second question");
      await vi.advanceTimersByTimeAsync(400);
    });

    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
    // Not dropped: it is sitting in the composer, on the chat screen, under a
    // loader that says why it has not gone yet.
    expect(result.current.state.chatInput).toBe("Second question");
    expect(result.current.state.screen).toBe("chat");
    expect(result.current.state.messages.filter((m) => m.role === "user")).toHaveLength(1);
  });

  it("never clobbers a draft the reader already has in the composer", async () => {
    mockAskQuestion.mockImplementation(() => new Promise<AskResponse>(() => {}));
    const { result } = renderHook(() => useLandingState());
    await askAndLeave(result);
    act(() => {
      result.current.onChatInput({
        target: { value: "my own draft" },
      } as React.ChangeEvent<HTMLInputElement>);
    });

    await act(async () => {
      result.current.enterChat("Second question");
      await vi.advanceTimersByTimeAsync(400);
    });

    expect(result.current.state.chatInput).toBe("my own draft");
    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
  });

  it("parks the second of TWO landing entry points fired inside the 60ms send delay", async () => {
    // The gap the first version of this fix left open. `enterChat` reads
    // `inFlight.current` synchronously, but the send is deferred 60ms and
    // `markInFlight` only runs once `sendLive` starts — so two entry points
    // fired inside that window both read 0 and both sent. Measured on the
    // first fix: asks=2, transcript [Q1][Q2][A2][A1], which is exactly the
    // attribution defect #62 exists to prevent.
    mockAskQuestion.mockImplementation(() => new Promise<AskResponse>(() => {}));
    const { result } = renderHook(() => useLandingState());

    await act(async () => {
      result.current.enterChat("Question one");
      await vi.advanceTimersByTimeAsync(30);
      result.current.enterChat("Question two");
      await vi.advanceTimersByTimeAsync(400);
    });

    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
    expect(result.current.state.pending).toBe(1);
    expect(result.current.state.messages.filter((m) => m.role === "user")).toHaveLength(1);
    expect(result.current.state.chatInput).toBe("Question two");
  });

  it("parks the second of two fired in the SAME tick", async () => {
    mockAskQuestion.mockImplementation(() => new Promise<AskResponse>(() => {}));
    const { result } = renderHook(() => useLandingState());

    await act(async () => {
      result.current.enterChat("Question one");
      result.current.enterChat("Question two");
      await vi.advanceTimersByTimeAsync(400);
    });

    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
  });

  it("still sends normally from a landing entry point when nothing is in flight", async () => {
    // Partner for both tests above: proves the parking branch has not simply
    // disabled the landing entry points.
    mockAskQuestion.mockResolvedValue(askResponse({ answer: "Landing answer." }));
    const { result } = renderHook(() => useLandingState());

    await act(async () => {
      result.current.enterChat("A landing question");
      await vi.advanceTimersByTimeAsync(4000);
    });

    expect(mockAskQuestion).toHaveBeenCalledTimes(1);
    expect(result.current.state.messages.map((m) => m.text)).toEqual([
      "A landing question",
      "Landing answer.",
    ]);
    expect(result.current.state.chatInput).toBe("");
  });
});
