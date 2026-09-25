/**
 * Trust must-haves on a rendered answer (ADR-0005 §6): the "docs as of" stamp on
 * the answer and on each source card, "Copy as Markdown", and click-to-copy code.
 * These ship without the access setting.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { AnswerBody } from "./AnswerBody";
import type { Source } from "../data/knowledgeBase";

const sources: Source[] = [
  { n: "1", title: "Model config", url: "https://d/model", asOf: "2026-09-20" },
  { n: "2", title: "CLI flags", url: "https://d/cli", asOf: "2026-09-01" },
];

let writeText: ReturnType<typeof vi.fn>;
beforeEach(() => {
  writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});
afterEach(() => vi.restoreAllMocks());

describe("docs-as-of stamps", () => {
  it("dates the answer by its OLDEST source and each card by its own", () => {
    // Turns red if: the answer stamp uses the newest date, or a card loses its date.
    render(<AnswerBody text="Use `--model` [1] and [2]." sources={sources} />);
    expect(screen.getByText("Docs as of 1 Sep 2026")).toBeTruthy();
    expect(screen.getByText("Source as of 20 Sep 2026")).toBeTruthy();
    expect(screen.getByText("Source as of 1 Sep 2026")).toBeTruthy();
  });

  it("shows no stamp when no source carries a date", () => {
    // Turns red if: a missing date is rendered as a made-up one.
    render(<AnswerBody text="x [1]" sources={[{ n: "1", title: "A", url: "/a" }]} />);
    expect(screen.queryByText(/Docs as of/)).toBeNull();
    expect(screen.queryByText(/Source as of/)).toBeNull();
    expect(screen.getByText("A")).toBeTruthy(); // partner: the card rendered
  });

  it("shows nothing while the answer is still streaming", () => {
    // Turns red if: a half-written answer is stamped before its sources exist.
    render(<AnswerBody text="x [1]" streaming sources={sources} />);
    expect(screen.queryByText(/Docs as of/)).toBeNull();
  });
});

describe("copy", () => {
  it("copies the answer as Markdown with its sources, and says so", async () => {
    // Turns red if: the button is missing, copies the wrong text, or never
    // announces the result to a screen reader.
    render(<AnswerBody text="Use `--model` [1] and [2]." sources={sources} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy as Markdown" }));
    });
    expect(writeText).toHaveBeenCalledTimes(1);
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied.startsWith("Use `--model` [1] and [2].\n\nSources (docs as of 1 Sep 2026):")).toBe(true);
    expect(copied).toContain("- [1] [Model config](https://d/model) — docs as of 20 Sep 2026");
    expect(screen.getByTestId("copy-status").textContent).toBe("Copied");
  });

  it("copies a code span when it is clicked", async () => {
    // Turns red if: code spans stop being copy buttons, or copy the wrong value.
    render(<AnswerBody text="Run `claude --model opus` now." sources={[]} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "claude --model opus (copy)" }));
    });
    expect(writeText).toHaveBeenCalledWith("claude --model opus");
    expect(screen.getByTestId("copy-status").textContent).toBe("Copied");
  });

  it("says so when the clipboard refuses", async () => {
    // Turns red if: a failed copy is silently reported as success.
    writeText.mockRejectedValueOnce(new Error("denied"));
    render(<AnswerBody text="x" sources={[]} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy as Markdown" }));
    });
    expect(screen.getByTestId("copy-status").textContent).toBe("Copy failed");
  });

  it("offers no copy button while the answer is still streaming", () => {
    // Turns red if: a half-written answer can be copied.
    render(<AnswerBody text="x" streaming sources={[]} />);
    expect(screen.queryByRole("button", { name: "Copy as Markdown" })).toBeNull();
  });
});

describe("groupSources — one card, one date", () => {
  it("keeps the OLDEST date when citations of one page disagree", async () => {
    // Rare (one page has one fetch date), but reachable while two indexes are
    // active. Turns red if: the card keeps whichever date it saw first.
    const { groupSources } = await import("./AnswerBody");
    const [g] = groupSources([
      { n: "1", title: "A", url: "/a", asOf: "2026-09-20" },
      { n: "2", title: "A", url: "/a", asOf: "2026-09-01" },
    ]);
    expect(g.asOf).toBe("2026-09-01");
    expect(g.markers).toEqual(["1", "2"]); // partner: they really were grouped
  });
});

describe("copy — edge cases from review", () => {
  it("reports a MISSING clipboard as a failure instead of throwing", async () => {
    // Plain-http origins and some iframes have no navigator.clipboard at all.
    // Turns red if: the handler calls writeText unguarded (a TypeError, and no
    // "Copy failed" announcement).
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    render(<AnswerBody text="x" sources={[]} />);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy as Markdown" }));
    });
    expect(screen.getByTestId("copy-status").textContent).toBe("Copy failed");
  });

  it("re-announces a second copy", async () => {
    // A live region only speaks when its text CHANGES. Turns red if: the status
    // is not cleared before each copy, so copy #2 is silent.
    render(<AnswerBody text="x" sources={[]} />);
    const status = screen.getByTestId("copy-status");
    const seen: string[] = [];
    const obs = new MutationObserver(() => seen.push(status.textContent ?? ""));
    obs.observe(status, { childList: true, characterData: true, subtree: true });
    const btn = screen.getByRole("button", { name: "Copy as Markdown" });
    await act(async () => fireEvent.click(btn));
    await act(async () => fireEvent.click(btn));
    obs.disconnect();
    expect(seen.filter((t) => t === "Copied")).toHaveLength(2);
  });

  it("names a code button by its code, so the sentence still reads as text", () => {
    // Turns red if: an aria-label replaces the code ("Copy code: …").
    render(<AnswerBody text="Run `ls -la` now." sources={[]} />);
    const btn = screen.getByRole("button", { name: "ls -la (copy)" });
    expect(btn.getAttribute("aria-label")).toBeNull();
    expect(btn.querySelector("code")?.textContent).toBe("ls -la");
  });
});

