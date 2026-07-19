import { describe, it, expect } from "vitest";
import { mentionsCitevyn, CITEVYN_ALIASES } from "./citevynAliases";

// These cases are lifted from backend/tests/test_guardrails_domain.py on purpose:
// the whole point of this module is that the offline path answers the same
// questions the live guardrail does (#84 item 4). If a case here and its backend
// twin ever disagree, one of the two matchers has drifted.

describe("mentionsCitevyn — canonical spelling", () => {
  it.each([
    "What is CiteVyn?",
    "citevyn pro pricing",
    "CITEVYN plans",
    // Un-guarded canonical branch: these routed to citevyn before aliases
    // existed and must keep doing so.
    "is citevyn.com free?",
    "email support@citevyn please",
    "anti-citevyn rant",
  ])("recognizes %j", (q) => {
    expect(mentionsCitevyn(q)).toBe(true);
  });
});

describe("mentionsCitevyn — speech-to-text aliases", () => {
  it.each([
    "what is sitewin?",
    "what is citevin?",
    "is sitevyn free?",
    "what does sightwin cost",
    "what is cite vyn?",
    "what is site-vyn?",
    // Sentence-final alias is still the product, not a filename.
    "I was reading about sitewin.",
  ])("recognizes %j", (q) => {
    expect(mentionsCitevyn(q)).toBe(true);
  });
});

describe("mentionsCitevyn — deliberate misses", () => {
  it.each([
    // Two ordinary English words. Live these go to an LLM intent check; offline
    // there is no model to ask, so they are a deliberate, tested miss.
    "may the best site win!",
    "did the site win the award?",
    "what is our site win rate?",
    // "vin" is an ordinary noun (and French for wine) — separated spellings are out.
    "please cite VIN and mileage",
    "le site vin est en panne",
    // Identifiers: the alias is the string the user is asking ABOUT.
    "why does sitewin.example.com return 502?",
    "sitewin@example.com is the contact",
    "SITEWIN-1234 is blocked",
    "the sitewin/main branch",
    "docs.sitewin",
    // Ordinary off-domain questions.
    "How do I get a Gemini API key?",
    "",
  ])("does not fire on %j", (q) => {
    expect(mentionsCitevyn(q)).toBe(false);
  });
});

describe("mentionsCitevyn — statefulness", () => {
  it("returns the same answer on repeated calls", () => {
    // A /g/ regex would carry lastIndex between calls and alternate true/false.
    for (let i = 0; i < 4; i++) expect(mentionsCitevyn("what is sitewin?")).toBe(true);
  });
});

describe("CITEVYN_ALIASES", () => {
  it("mirrors the backend list exactly", () => {
    // The backend pins the same strings (test_guardrails_domain.py parses THIS
    // file), so a one-sided edit fails on one side or the other.
    expect(CITEVYN_ALIASES).toEqual([
      "citevin",
      "citewin",
      "sitevyn",
      "sitevin",
      "sitewin",
      "sightvyn",
      "sightvin",
      "sightwin",
      "cite[ \\t-]vyn",
      "site[ \\t-]vyn",
      "sight[ \\t-]vyn",
    ]);
  });
});
