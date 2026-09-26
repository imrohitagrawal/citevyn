/**
 * ADR-0005 Phase 5B: the chat with the access model on.
 *
 * - An anonymous visitor never gets a live (paid) answer: the chat says a free
 *   account is needed (the landing page's demo is the canned sample). Not a
 *   canned KB answer: the nearest canned answer to an unrelated question would
 *   mislead, and it would pull the whole canned knowledge base into the eager
 *   bundle of a live build (measured: +2.3 KB raw). Signed-in callers ask live.
 * - Each allowance refusal gets its own honest copy: none says "try again in a
 *   moment", because none of them clears by waiting a moment (TEST_STRATEGY §8b:
 *   a permanent 4xx must not read as temporary).
 * - With the access model OFF, nothing changes (today's behaviour is the partner
 *   of every case).
 *
 * Each test says which change turns it red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useLandingState } from "./useLandingState";
import { askQuestion, createSession, isLiveMode } from "../lib/api";
import { getAuthSnapshot } from "../lib/authStore";
import { getClientConfig, type ClientConfig } from "../lib/clientConfig";
import { ApiClientError } from "../lib/types";

vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => true),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
  getSession: vi.fn(),
}));
vi.mock("../lib/authStore", () => ({
  getAuthSnapshot: vi.fn(() => ({ status: "anonymous", user: null })),
}));
vi.mock("../lib/clientConfig", () => ({
  getClientConfig: vi.fn(() => ({ access_model: false, bot_check: null })),
}));

const ON: ClientConfig = {
  access_model: true,
  bot_check: { kind: "pow" },
  allowance: { free_trial_answers: 25, pro_monthly_answers: 1000 },
};
const OFF: ClientConfig = { access_model: false, bot_check: null, allowance: null };

/** Long enough for the fetch and the word streaming, shorter than a toast's
 * 5-second life (the same helper as useLandingState.test.tsx). */
async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(4000);
  });
}

function refusal(status: number, code: string): ApiClientError {
  return new ApiClientError("x", status, {
    request_id: "r",
    status: "error",
    error: { code, message: "server copy" },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(isLiveMode).mockReturnValue(true);
  vi.mocked(createSession).mockResolvedValue({
    request_id: "r",
    session_id: "sess-1",
    expires_at: "2026-10-11T12:00:00Z",
  });
  vi.mocked(getClientConfig).mockReturnValue(OFF);
  vi.mocked(getAuthSnapshot).mockReturnValue({ status: "anonymous", user: null });
});

afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe("anonymous visitors with the access model on", () => {
  it("never get a live answer; the chat tells them a free account is needed", async () => {
    // Turns red if: an anonymous question reaches the backend with the model on.
    vi.mocked(getClientConfig).mockReturnValue(ON);
    const { result } = renderHook(() => useLandingState());
    act(() => result.current.send("How do permissions work?"));
    await settle();
    expect(askQuestion).not.toHaveBeenCalled();
    const bot = result.current.state.messages[1];
    expect(bot.role).toBe("bot");
    expect(bot.text).toMatch(/sign in/i);
    expect(bot.text).toMatch(/free/i);
    expect(bot.refusal).toBe(false); // not a "NO SOURCE" content refusal
  });

  it("after signing in, the same question goes live (it is not locked out)", async () => {
    // Found in review: the sign-in reply did not mark the question retryable,
    // so the duplicate guard flashed the old bubble and never asked. Turns red
    // if: the reply is not recorded as retryable.
    vi.mocked(getClientConfig).mockReturnValue(ON);
    vi.mocked(askQuestion).mockResolvedValue({
      request_id: "r",
      message_id: "m",
      answer: "Live answer.",
      citations: [{ source_name: "S", title: "T", url: "https://x", chunk_id: "c", marker: 1 }],
      domain: "claude_code",
      intent: "how_to",
      confidence: "high",
      cache_hit: false,
      retrieval_strategy: "hybrid",
      unsupported: false,
      no_answer: false,
      source_version_hash: "h",
      answer_policy_version: "v1",
    } as unknown as Awaited<ReturnType<typeof askQuestion>>);
    const { result } = renderHook(() => useLandingState());
    act(() => result.current.send("How do permissions work?"));
    await settle();
    expect(askQuestion).not.toHaveBeenCalled();
    vi.mocked(getAuthSnapshot).mockReturnValue({
      status: "signed-in",
      user: { user_id: "usr_1", email: "a@x.com" },
    } as ReturnType<typeof getAuthSnapshot>);
    act(() => result.current.send("How do permissions work?"));
    await settle();
    expect(askQuestion).toHaveBeenCalledTimes(1);
  });

  it("partner: with the access model off they ask live, exactly as today", async () => {
    // Turns red if: the sample path is taken while the model is off.
    const { result } = renderHook(() => useLandingState());
    act(() => result.current.send("How do permissions work?"));
    await settle();
    expect(askQuestion).toHaveBeenCalledTimes(1);
  });

  it("partner: a signed-in caller asks live with the model on", async () => {
    // Turns red if: signed-in callers are sent to the sample too.
    vi.mocked(getClientConfig).mockReturnValue(ON);
    vi.mocked(getAuthSnapshot).mockReturnValue({
      status: "signed-in",
      user: { user_id: "usr_1", email: "a@x.com" },
    } as ReturnType<typeof getAuthSnapshot>);
    const { result } = renderHook(() => useLandingState());
    act(() => result.current.send("How do permissions work?"));
    await settle();
    expect(askQuestion).toHaveBeenCalledTimes(1);
  });
});

describe("allowance refusals get their own honest copy", () => {
  async function refusedWith(err: ApiClientError, config = ON) {
    vi.mocked(getClientConfig).mockReturnValue(config);
    vi.mocked(getAuthSnapshot).mockReturnValue({
      status: "signed-in",
      user: { user_id: "usr_1", email: "a@x.com" },
    } as ReturnType<typeof getAuthSnapshot>);
    vi.mocked(askQuestion).mockRejectedValue(err);
    const { result } = renderHook(() => useLandingState());
    act(() => result.current.send("A question"));
    await settle();
    const bot = result.current.state.messages[1];
    return { bot, toast: result.current.toasts[0] };
  }

  it("a used-up free trial says upgrade, not 'try again'", async () => {
    // Turns red if: plan_required falls through to the generic "try again" copy.
    const { bot, toast } = await refusedWith(refusal(403, "plan_required"));
    expect(bot.text).toMatch(/free questions/i);
    expect(bot.text).toMatch(/upgrade/i);
    expect(bot.text).not.toMatch(/try again/i);
    expect(bot.errorKind).toBe("rejected");
    expect(toast.title).toBe("Free questions used");
  });

  it("a used-up Pro month says when it resets, not 'too quickly'", async () => {
    // Turns red if: quota_exceeded (a 429) is shown as the hourly rate limit.
    const { bot, toast } = await refusedWith(refusal(429, "quota_exceeded"));
    expect(bot.text).toMatch(/this month/i);
    expect(bot.text).not.toMatch(/too quickly/i);
    expect(bot.errorKind).toBe("rejected");
    expect(toast.title).toBe("Monthly questions used");
  });

  it("an unverified account is told to verify its email", async () => {
    // Turns red if: verification_required falls through to the generic copy.
    const { bot, toast } = await refusedWith(refusal(403, "verification_required"));
    expect(bot.text).toMatch(/verify your email/i);
    expect(bot.text).not.toMatch(/try again/i);
    expect(toast.title).toBe("Verify your email");
  });

  it("an auth_required 401 says sign in when the model is on", async () => {
    // Turns red if: a signed-out caller is told the deployment is broken.
    const { bot } = await refusedWith(refusal(401, "auth_required"));
    expect(bot.text).toMatch(/sign in/i);
    expect(bot.text).not.toMatch(/not accepting requests/i);
  });

  it("partner: with the model off a 401 keeps today's 'not accepting requests' copy", async () => {
    // A bad client key also answers 401 auth_required (release v6). Turns red
    // if: the sign-in copy replaces it while the model is off.
    const { bot } = await refusedWith(refusal(401, "auth_required"), OFF);
    expect(bot.text).toMatch(/not accepting requests/i);
  });

  it("partner: the hourly rate limit keeps its own copy", async () => {
    // Turns red if: a plain 429 rate_limited is swallowed by the quota branch.
    const { bot } = await refusedWith(refusal(429, "rate_limited"));
    expect(bot.text).toMatch(/too quickly/i);
    expect(bot.errorKind).toBe("rate_limit");
  });
});
