/**
 * "Copy answer as Markdown" (ADR-0005 §6 trust must-haves).
 *
 * The copied text must stand on its own once pasted somewhere else: the answer
 * with its [n] markers, then every source the markers point at, with its link
 * and the date its docs were fetched. Turns red if: a source, its link, its date
 * or the answer-level date is dropped, or markers are renumbered.
 */
import { describe, expect, it } from "vitest";
import { answerMarkdown, formatDocsDate, oldestDate } from "./answerMarkdown";

describe("answerMarkdown", () => {
  it("appends each source with its link and date, and the answer's date", () => {
    const md = answerMarkdown("Use `--model` [1]. Also see [3].", [
      { title: "Model config", url: "https://d/model", markers: ["1"], asOf: "2026-09-20" },
      { title: "CLI flags", url: "https://d/cli", markers: ["3"], asOf: "2026-09-01" },
    ]);
    expect(md).toBe(
      [
        "Use `--model` [1]. Also see [3].",
        "",
        "Sources (docs as of 1 Sep 2026):",
        "- [1] [Model config](https://d/model) — docs as of 20 Sep 2026",
        "- [3] [CLI flags](https://d/cli) — docs as of 1 Sep 2026",
      ].join("\n"),
    );
  });

  it("omits dates it does not know and links it cannot make", () => {
    const md = answerMarkdown("x [1] [2]", [
      { title: "A", url: "", markers: ["1", "2"] },
    ]);
    expect(md).toBe(["x [1] [2]", "", "Sources:", "- [1, 2] A"].join("\n"));
  });

  it("is just the answer when there are no sources", () => {
    expect(answerMarkdown("No source.", [])).toBe("No source.");
  });
});

describe("dates", () => {
  it("formats an ISO day in UTC, not the reader's zone", () => {
    expect(formatDocsDate("2026-09-01")).toBe("1 Sep 2026");
  });

  it("picks the oldest known date and ignores unknowns", () => {
    expect(oldestDate(["2026-09-20", undefined, "2026-09-01"])).toBe("2026-09-01");
    expect(oldestDate([undefined])).toBeUndefined();
    expect(oldestDate([])).toBeUndefined();
  });
});

describe("answerMarkdown — hostile titles and links", () => {
  it("escapes brackets in a title and drops an unsafe link", () => {
    // Turns red if: a ] in a title ends the link early, or a javascript: URL is
    // written into the copied Markdown as a link.
    const md = answerMarkdown("x [1] [2]", [
      { title: "Arrays [T]", url: "https://d/a", markers: ["1"] },
      { title: "Bad", url: "javascript:alert(1)", markers: ["2"] },
    ]);
    expect(md).toContain("- [1] [Arrays \\[T\\]](https://d/a)");
    expect(md).toContain("- [2] Bad");
    expect(md).not.toContain("javascript:");
  });
});

