import { describe, it, expect } from "vitest";
import { matchCitevynMeta } from "./knowledgeBase";

describe("matchCitevynMeta", () => {
  it("answers CiteVyn Pro / membership questions from built-in copy", () => {
    const pro = matchCitevynMeta("What do I get with CiteVyn Pro?");
    expect(pro).not.toBeNull();
    expect(pro?.a.toLowerCase()).toContain("pro");
    // membership phrasing resolves to the same Pro answer
    expect(matchCitevynMeta("Is CiteVyn membership worth it?")?.a).toBe(pro?.a);
  });

  it("answers coverage / trust / freshness questions about CiteVyn", () => {
    expect(matchCitevynMeta("Which tools does CiteVyn cover?")?.a).toMatch(/Claude|Codex|Gemini/);
    expect(matchCitevynMeta("Does CiteVyn hallucinate?")?.a.toLowerCase()).toContain("hallucinate");
    expect(matchCitevynMeta("How fresh is CiteVyn's index?")?.a.toLowerCase()).toContain("index");
  });

  it("returns a generic answer for other CiteVyn-about questions", () => {
    const g = matchCitevynMeta("What is CiteVyn?");
    expect(g).not.toBeNull();
    expect(g?.a.toLowerCase()).toContain("citevyn");
  });

  it("returns null for product questions so they reach the backend", () => {
    // These mention the documented products, not CiteVyn itself.
    expect(matchCitevynMeta("How do I get a Gemini API key?")).toBeNull();
    expect(matchCitevynMeta("Does Claude Code cost money?")).toBeNull();
    expect(matchCitevynMeta("What does the --model flag do in Codex?")).toBeNull();
  });

  it("is case-insensitive on the CiteVyn guard", () => {
    expect(matchCitevynMeta("citevyn pro pricing")).not.toBeNull();
    expect(matchCitevynMeta("CITEVYN plans")).not.toBeNull();
  });
});
