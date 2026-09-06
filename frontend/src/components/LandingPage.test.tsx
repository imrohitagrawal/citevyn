import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { LandingPage } from "./LandingPage";
import { KB } from "../data/knowledgeBase";
import { isLiveMode, createSession, askQuestion } from "../lib/api";
import type { AskResponse } from "../lib/types";

// The interactive demo answers from the built-in KB (no network), but the hook
// still imports the api module — mock it so nothing tries to reach a backend and
// the landing (offline) demo path is exercised deterministically.
vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => false),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
  // Header renders AccountMenu -> useAuth -> authStore, which imports these
  // even on the offline landing demo path (ADR-0004 PR 8).
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(isLiveMode).mockReturnValue(false);
  vi.mocked(createSession).mockReset();
  vi.mocked(askQuestion).mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("LandingPage — interactive demo question/answer sync", () => {
  it("shows the SELECTED demo question above its answer, not the hero's rotating question", async () => {
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);

    // Pick the Gemini-streaming demo. Its question is deliberately NOT one of the
    // hero's rotating placeholders (HERO_ORDER = claude-code / gemini-key /
    // codex-flag), so if the panel's question header were (buggily) bound to the
    // hero question — as it was before this fix — it could NEVER display this text.
    const selected = KB["gemini-stream"];
    const btn = Array.from(container.querySelectorAll(".demo-q-btn")).find((b) =>
      b.textContent?.includes(selected.q),
    );
    expect(btn, "the demo question button should render").toBeTruthy();

    act(() => {
      fireEvent.click(btn!);
    });

    // The question shown ABOVE the answer must be the one the user selected —
    // the regression being that it showed an unrelated (hero) question instead.
    const header = container.querySelector(".demo-question-row p");
    expect(header?.textContent).toBe(selected.q);

    // …and the streamed answer belongs to the same demo, so header and body agree.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    const answer = container.querySelector(".demo-answer");
    expect(answer?.textContent).toContain("streaming variant");
  });
});

/**
 * `state.pending` is a COUNT of in-flight requests; `ChatView` takes a boolean.
 * The `> 0` at the seam is the only thing between the two, and a count is
 * always `>= 0` — so a mutation there shows the loader on every chat screen
 * forever. Demo mode never sets the count, which makes it the clean control.
 */
describe("LandingPage — the loading indicator is wired to the COUNT, not to its presence", () => {
  it("shows no 'Searching the docs…' bubble on a demo-mode chat screen", async () => {
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    const hero = container.querySelector("#hero-input") as HTMLInputElement;

    act(() => {
      fireEvent.change(hero, { target: { value: "What is Claude Code?" } });
      fireEvent.keyDown(hero, { key: "Enter" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });

    // Partner: we really are on the chat screen with a question in it, so the
    // absence below is not the absence of the whole view.
    expect(container.querySelector(".chat-screen")).toBeTruthy();
    expect(container.querySelector(".message.user-msg")?.textContent).toContain(
      "What is Claude Code?",
    );
    expect(container.querySelector(".pending-bubble")).toBeNull();
    expect(container.textContent).not.toContain("Searching the docs…");
  });
});

/**
 * The POSITIVE half of the seam. The demo-mode test above only proves the
 * loader is absent when it should be; on its own, `pending={false}` — cutting
 * the wire entirely — and `state.pending > 1` both survived the whole suite.
 * This proves the loader can actually reach the screen through that seam.
 */
describe("LandingPage — the loading indicator reaches the screen in live mode", () => {
  it("shows 'Searching the docs…' while the request is open, and clears it when the answer lands", async () => {
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(createSession).mockResolvedValue({
      request_id: "r",
      session_id: "s",
      expires_at: "2026-07-11T12:00:00Z",
    });
    let release: (r: AskResponse) => void = () => {};
    vi.mocked(askQuestion).mockImplementation(
      () => new Promise<AskResponse>((resolve) => { release = resolve; }),
    );

    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    const hero = container.querySelector("#hero-input") as HTMLInputElement;
    act(() => {
      fireEvent.change(hero, { target: { value: "What is Claude Code?" } });
      fireEvent.keyDown(hero, { key: "Enter" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });

    expect(container.querySelector(".pending-bubble")).not.toBeNull();
    expect(container.textContent).toContain("Searching the docs…");
    expect(container.querySelector(".send-button")?.getAttribute("aria-disabled")).toBe("true");

    await act(async () => {
      release({
        request_id: "r2",
        message_id: "m",
        answer: "Claude Code is an agentic coding tool.",
        citations: [],
        domain: "claude_code",
        intent: "how_to",
        confidence: "high",
        cache_hit: false,
        retrieval_strategy: "hybrid_reranked",
        unsupported: false,
        no_answer: false,
        source_version_hash: "h",
        answer_policy_version: "v1",
      });
      await vi.advanceTimersByTimeAsync(6000);
    });

    expect(container.querySelector(".pending-bubble")).toBeNull();
    expect(container.querySelector(".send-button")?.getAttribute("aria-disabled")).not.toBe("true");
  });
});
