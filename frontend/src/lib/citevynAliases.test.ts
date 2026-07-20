import { describe, it, expect } from "vitest";
import { mentionsCitevyn, CITEVYN_ALIASES } from "./citevynAliases";
// `?raw` gives the module's own SOURCE TEXT. The pattern is module-private and
// this must assert on what is written, not on what it matches (see below).
import aliasModuleSource from "./citevynAliases.ts?raw";
import parityCorpus from "./citevynAliases.cases.json";

// The behavioural cases live in citevynAliases.cases.json, NOT inline here, and
// backend/tests/test_guardrails_domain.py runs the very same file against
// _CITEVYN_RE. Two suites over one corpus is what makes "mirror" a checked claim
// instead of a comment (#84 item 4) — the earlier inline copies could drift, and
// did: the ASCII-vs-Unicode cases below passed here while the backend refused.

describe("mentionsCitevyn — shared cross-language parity corpus", () => {
  it.each(parityCorpus.cases.map((c) => [c.q, c.match, c.why] as const))(
    "%j -> %s (%s)",
    (q, expected) => {
      expect(mentionsCitevyn(q)).toBe(expected);
    },
  );

  it("covers both outcomes, so an all-true or all-false matcher cannot pass", () => {
    // Guards the corpus itself: a bulk edit that flipped every expectation to one
    // value would leave the suite green against a constant function.
    expect(parityCorpus.cases.some((c) => c.match)).toBe(true);
    expect(parityCorpus.cases.some((c) => !c.match)).toBe(true);
  });
});

describe("mentionsCitevyn — no regex lookbehind", () => {
  // WHY a source assertion and not a behavioural one: on V8 (which runs both
  // vitest and the Playwright chromium suite) a lookbehind works perfectly, so
  // NO test that calls mentionsCitevyn can ever catch its reintroduction. The
  // failure only exists on engines we do not run tests on.
  //
  // And it is a hard failure, not a degradation: the pattern is built with
  // `new RegExp` at MODULE LOAD, so an unsupported construct throws a
  // SyntaxError while the module is evaluating and the bundle never boots. The
  // string form also hides the pattern from esbuild, which can neither warn nor
  // downlevel. The project declares no browserslist, so its floor is Vite 6's
  // default build target ("baseline-widely-available" = safari16.0) and Safari
  // only gained lookbehind in 16.4 — inside the supported range.
  const source = aliasModuleSource;

  it("does not construct a lookbehind, which is below the browser baseline", () => {
    expect(source).not.toMatch(/\(\?<[=!]/);
  });

  it("still guards the leading side, so dropping the lookbehind did not drop the guard", () => {
    // Removing `(?<!...)` without replacing it would silently re-open
    // "docs.sitewin". The consumed-alternation replacement must be present.
    expect(source).toContain("(?:^|[^${IDENTIFIER_CHAR}])");
  });

  it("compiles the pattern with the `u` flag the \\p{...} classes require", () => {
    // Without `u`, `\p{L}` is the literal "p{L}" and every guard silently rots
    // into a character class that matches "p", "{", "L", "}".
    expect(source).toMatch(/"iu"/);
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

describe("mentionsCitevyn — statefulness", () => {
  it("returns the same answer on repeated calls", () => {
    // A /g/ regex would carry lastIndex between calls and alternate true/false.
    for (let i = 0; i < 4; i++) expect(mentionsCitevyn("what is sitewin?")).toBe(true);
  });
});
