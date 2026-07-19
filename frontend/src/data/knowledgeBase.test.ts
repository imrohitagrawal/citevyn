import { describe, it, expect } from "vitest";
import { matchCitevynMeta, GENERIC_REFUSAL } from "./knowledgeBase";

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

  // --- #84 item 4: offline path must recognize the same names as the backend ---

  it("answers speech-to-text manglings of the product name", () => {
    // Regression: the guard was a bare includes("citevyn"), so the owner's
    // dictation ("what is sitewin?") fell through to matchKB and got the generic
    // refusal offline while the live backend answered it.
    expect(matchCitevynMeta("what is sitewin?")).not.toBeNull();
    expect(matchCitevynMeta("is sitevyn free?")?.a).toBe(
      matchCitevynMeta("is CiteVyn free?")?.a,
    );
    expect(matchCitevynMeta("what tools does citevin cover?")?.a).toBe(
      matchCitevynMeta("what tools does CiteVyn cover?")?.a,
    );
  });

  it("does not treat an alias inside an identifier as the product", () => {
    // "sitewin.example.com" is the string the user is asking ABOUT.
    expect(matchCitevynMeta("why does sitewin.example.com return 502?")).toBeNull();
    // Two ordinary English words — a deliberate miss offline (no model to ask).
    expect(matchCitevynMeta("may the best site win!")).toBeNull();
  });
});

describe("GENERIC_REFUSAL", () => {
  it("nudges toward CiteVyn-meta questions, like the backend refusal", () => {
    // #84 item 5 — a refused near-miss meta question ("what is Pro?") otherwise
    // gives the user no hint that naming the product is the phrasing that works.
    expect(GENERIC_REFUSAL).toContain("CiteVyn itself");
    // The four products stay named first: this is a scope statement, not an upsell.
    expect(GENERIC_REFUSAL.indexOf("Claude")).toBeLessThan(
      GENERIC_REFUSAL.indexOf("CiteVyn itself"),
    );
  });
});
