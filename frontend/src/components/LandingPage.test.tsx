import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { LandingPage } from "./LandingPage";
import { KB } from "../data/knowledgeBase";
import { isLiveMode, createSession, askQuestion } from "../lib/api";

// The interactive demo answers from the built-in KB (no network), but the hook
// still imports the api module — mock it so nothing tries to reach a backend and
// the landing (offline) demo path is exercised deterministically.
vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => false),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
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
