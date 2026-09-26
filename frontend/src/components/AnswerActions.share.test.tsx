/**
 * "Share" under an answer (ADR-0005 Phase 6C-2). Only with the access model on,
 * only for an answer with sources. Each test says which change turns it red.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import AnswerActions from "./AnswerActions";
import { useClientConfig } from "../lib/clientConfig";
import { shareAnswer } from "../lib/shares";
import { ApiClientError } from "../lib/types";

vi.mock("../lib/clientConfig", () => ({ useClientConfig: vi.fn() }));
vi.mock("../lib/shares", async (orig) => ({
  ...(await orig<typeof import("../lib/shares")>()),
  shareAnswer: vi.fn(),
}));

const REF = { sessionId: "s1", messageId: "m1", question: "How do I set the model?" };
const ID = "0123456789abcdef0123456789abcdef";
const SHARE = { share_id: ID, url: `/s/${ID}`, question: REF.question, created_at: "2026-09-26T10:00:00Z" };
let writeText: ReturnType<typeof vi.fn>;

function modelOn(on = true) {
  vi.mocked(useClientConfig).mockReturnValue({ access_model: on } as ReturnType<typeof useClientConfig>);
}

beforeEach(() => {
  modelOn();
  writeText = vi.fn(() => Promise.resolve());
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

/** userEvent.setup() installs its own clipboard, so ours goes in after it. */
function setup() {
  const user = userEvent.setup();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  return user;
}

const status = () => screen.getByTestId("answer-actions-status").textContent;

describe("sharing an answer", () => {
  it("shares THIS answer, copies its link and says who can see it", async () => {
    // Turns red if: the wrong answer is shared, the link is not copied, or the
    // reader is not told the link is public and outlives the chat.
    vi.mocked(shareAnswer).mockResolvedValue(SHARE);
    render(<AnswerActions {...REF} shareable />);
    await setup().click(screen.getByRole("button", { name: "Share" }));
    const link = `${window.location.origin}/s/${ID}`;
    expect(shareAnswer).toHaveBeenCalledWith("s1", "m1");
    expect(writeText).toHaveBeenCalledWith(link);
    expect(await screen.findByText(link)).toHaveAttribute("href", link);
    const note = screen.getByTestId("share-link").textContent ?? "";
    expect(note).toMatch(/anyone with this link can see/i);
    expect(note).toMatch(/deleting this chat does not remove it/i);
    expect(status()).toBe("Link copied.");
  });

  it("offers no Share with the access model off, or for an answer without sources", () => {
    // Turns red if: Share shows while the model is off, or on a refusal.
    modelOn(false);
    const { unmount } = render(<AnswerActions {...REF} shareable />);
    expect(screen.queryByRole("button", { name: "Share" })).toBeNull();
    unmount();
    modelOn(true);
    render(<AnswerActions {...REF} shareable={false} />);
    expect(screen.queryByRole("button", { name: "Share" })).toBeNull();
    expect(screen.getByRole("button", { name: "Helpful" })).toBeInTheDocument(); // partner
  });

  it("a Free account is told sharing is part of Pro", async () => {
    // Turns red if: plan_required gets the generic failure copy.
    vi.mocked(shareAnswer).mockRejectedValue(
      new ApiClientError("x", 403, {
        request_id: "r",
        status: "error",
        error: { code: "plan_required", message: "x" },
      }),
    );
    render(<AnswerActions {...REF} shareable />);
    await setup().click(screen.getByRole("button", { name: "Share" }));
    expect(status()).toBe("Sharing answers is part of Pro.");
    expect(screen.queryByTestId("share-link")).toBeNull();
  });

  it("never shows a link the server did not shape as /s/<id>", async () => {
    // Turns red if: the link is built from whatever url the server sent.
    vi.mocked(shareAnswer).mockResolvedValue({ ...SHARE, url: "https://evil.example/s/x" });
    render(<AnswerActions {...REF} shareable />);
    await setup().click(screen.getByRole("button", { name: "Share" }));
    expect(screen.queryByTestId("share-link")).toBeNull();
    expect(writeText).not.toHaveBeenCalled();
    expect(status()).toBe("Could not share that. Please try again.");
  });

  it("when copying fails, the link is still shown", async () => {
    // Turns red if: a clipboard failure claims "Link copied." or hides the link.
    writeText.mockRejectedValue(new Error("denied"));
    vi.mocked(shareAnswer).mockResolvedValue(SHARE);
    render(<AnswerActions {...REF} shareable />);
    await setup().click(screen.getByRole("button", { name: "Share" }));
    expect(await screen.findByTestId("share-link")).toBeInTheDocument();
    expect(status()).toBe("Link ready. Copy it below.");
  });

  it("two quick clicks send one request", async () => {
    // Turns red if: the in-flight guard is dropped.
    let finish: (v: typeof SHARE) => void = () => undefined;
    vi.mocked(shareAnswer).mockReturnValue(new Promise((r) => (finish = r)));
    render(<AnswerActions {...REF} shareable />);
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Share" }));
    await user.click(screen.getByRole("button", { name: "Share" }));
    expect(shareAnswer).toHaveBeenCalledTimes(1);
    finish(SHARE);
  });
});
