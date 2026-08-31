import { describe, it, expect } from "vitest";
import { citationsToSources } from "./citations";
import type { Citation } from "./types";

const citation = (over: Partial<Citation> = {}): Citation => ({
  source_name: "Claude Code Docs",
  title: "Permissions",
  url: "https://docs.claude.com/en/docs/claude-code/permissions",
  chunk_id: "chunk_123",
  marker: 1,
  ...over,
});

describe("citationsToSources", () => {
  it("maps API citations to demo Source shape with 1-based numbering", () => {
    const sources = citationsToSources([
      citation({ title: "Overview", url: "https://a", marker: 1 }),
      citation({ title: "Quickstart", url: "https://b", marker: 2 }),
    ]);

    expect(sources).toEqual([
      { n: "1", title: "Overview", url: "https://a" },
      { n: "2", title: "Quickstart", url: "https://b" },
    ]);
  });

  it("numbers by the model's marker, not by array position (#215)", () => {
    // An answer citing [1] and [3] returns TWO citations. Numbering them by
    // array position would label the cards 1 and 2 while the answer text still
    // says [3] — a marker pointing at a card that does not exist. This is the
    // one assertion that array-position numbering cannot satisfy.
    const sources = citationsToSources([
      citation({ title: "Overview", url: "https://a", marker: 1 }),
      citation({ title: "Rate limits", url: "https://c", marker: 3 }),
    ]);

    expect(sources.map((s) => s.n)).toEqual(["1", "3"]);
  });

  it("falls back to array position when the wire omits a usable marker", () => {
    // `api.ts` returns `parsed as T` — a bare cast with no wire validation — so
    // the required `marker` is a compile-time promise the network does not
    // keep. A malformed response must render a number, never `undefined`.
    const malformed = [
      { source_name: "A", title: "A", url: "https://a", chunk_id: "c1" },
      { source_name: "B", title: "B", url: "https://b", chunk_id: "c2" },
    ] as unknown as Parameters<typeof citationsToSources>[0];

    expect(citationsToSources(malformed).map((s) => s.n)).toEqual(["1", "2"]);
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
