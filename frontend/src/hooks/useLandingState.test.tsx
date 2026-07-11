import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useLandingState } from "./useLandingState";
import { askQuestion, createSession, isLiveMode } from "../lib/api";
import { ApiClientError } from "../lib/types";
import type { AskResponse, CreateSessionResponse } from "../lib/types";

// The hook talks to the backend through these three functions; mock the
// whole module so no real network happens and ``isLiveMode`` is
// controllable per test. ``citationsToSources`` (a pure adapter) and
// ``useToast`` stay real so the wiring is exercised end-to-end.
vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => true),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
}));

const mockIsLive = vi.mocked(isLiveMode);
const mockCreateSession = vi.mocked(createSession);
const mockAskQuestion = vi.mocked(askQuestion);

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
      { source_name: "Claude Code Docs", title: "Permissions", url: "https://x", chunk_id: "c1" },
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
});

describe("useLandingState — live error path", () => {
  it("surfaces a rate-limit as a toast plus an inline refusal message", async () => {
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
    expect(bot.refusal).toBe(true);
    expect(bot.text).toContain("Slow down.");

    expect(result.current.toasts).toHaveLength(1);
    expect(result.current.toasts[0]).toMatchObject({ kind: "error", title: "Rate limit reached" });
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

  it("labels a 5xx as a backend-unavailable error", async () => {
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

    expect(result.current.toasts[0]).toMatchObject({ kind: "error", title: "Backend unavailable" });
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
    expect(result.current.state.messages[1].refusal).toBe(true);
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
