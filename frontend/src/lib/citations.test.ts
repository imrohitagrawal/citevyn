import { describe, it, expect } from "vitest";
import { citationsToSources } from "./citations";
import type { Citation } from "./types";

const citation = (over: Partial<Citation> = {}): Citation => ({
  source_name: "Claude Code Docs",
  title: "Permissions",
  url: "https://docs.claude.com/en/docs/claude-code/permissions",
  chunk_id: "chunk_123",
  ...over,
});

describe("citationsToSources", () => {
  it("maps API citations to demo Source shape with 1-based numbering", () => {
    const sources = citationsToSources([
      citation({ title: "Overview", url: "https://a" }),
      citation({ title: "Quickstart", url: "https://b" }),
    ]);

    expect(sources).toEqual([
      { n: "1", title: "Overview", url: "https://a" },
      { n: "2", title: "Quickstart", url: "https://b" },
    ]);
  });

  it("returns an empty array for no citations", () => {
    expect(citationsToSources([])).toEqual([]);
  });

  it("falls back to source_name when title is empty", () => {
    const [source] = citationsToSources([
      citation({ title: "", source_name: "Gemini API Docs" }),
    ]);
    expect(source.title).toBe("Gemini API Docs");
  });

  it("falls back to a generic label when both title and source_name are empty", () => {
    const [source] = citationsToSources([
      citation({ title: "", source_name: "" }),
    ]);
    expect(source.title).toBe("Source 1");
  });

  it("tolerates a missing url by emitting an empty string", () => {
    const [source] = citationsToSources([
      citation({ url: undefined as unknown as string }),
    ]);
    expect(source.url).toBe("");
  });
});
