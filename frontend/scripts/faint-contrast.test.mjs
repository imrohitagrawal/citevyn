/**
 * #396 — `color: var(--faint)` is banned for text in `frontend/src`.
 *
 * WHY THE RULE IS UNCONDITIONAL, AND WHY THE NON-VACUITY PARTNER IS NOT A
 * `--faint` COUNT
 * ---------------------------------------------------------------------------
 * `--faint` measures 2.92:1 at BEST in the light theme (on `--surface`; 2.77 on
 * `--bg`, 2.58 on `--surface-2`) — under even WCAG's relaxed 3:1 LARGE-TEXT
 * floor. So the ban has no font-size condition, and after this change the token
 * has ZERO consumers.
 *
 * That second fact is a trap for the obvious guard. A partner that counted
 * `--faint` rules "to prove the checker can see things" would be VACUOUS BY
 * CONSTRUCTION: the count is zero forever, so the partner can never fail and
 * proves nothing. The partners here are derived from the file instead:
 *
 *   1. COMPLETENESS — the postcss walk's `color:` count must equal a second,
 *      independent textual count, both `> 0`, and every line that LOOKS like a
 *      `color:` declaration must be one the parser actually collected. A parser
 *      that bails out early is visible; a `>= 40`-style floor would not be
 *      (that is exactly how the BACKLOG guard rotted, #393).
 *   2. DETECTOR-STILL-WORKS — take the REAL stylesheet, swap `var(--muted)` for
 *      `var(--faint)` in memory, and assert the analyzer now reports many
 *      violations naming real selectors. Without it, "zero violations" and "the
 *      analyzer went blind" are the same green.
 *
 * ONE LINE PER TEST names the change that turns it red; each was made and
 * watched go red before this file was committed.
 */
import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";
import {
  blankOut,
  colorDeclarationLinesTextually,
  countColorDeclarationsTextually,
  findColorDeclarations,
  findFaintColorUses,
  findInlineFaintUses,
  findTextPaintDeclarations,
} from "./faint-contrast.mjs";

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Every file under `dir` whose name ends in one of `exts`. */
function walk(dir, exts) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      out.push(...walk(p, exts));
    } else if (exts.some((e) => name.endsWith(e))) {
      out.push(p);
    }
  }
  return out;
}

/**
 * The stylesheets this guard owns. `src/**` is the rule's stated scope;
 * `public/about.css` is added because the browser half CANNOT reach `/about`
 * (it is a static page outside the SPA), so this is the only net over it.
 */
const CSS_FILES = [
  ...walk(join(frontendRoot, "src"), [".css"]),
  join(frontendRoot, "public", "about.css"),
];

const SOURCE_FILES = walk(join(frontendRoot, "src"), [".tsx", ".ts"]);

const LANDING = join(frontendRoot, "src", "styles", "landing.css");
const landingText = readFileSync(LANDING, "utf8");

// ---------------------------------------------------------------------------
// The parser, on fixtures
// ---------------------------------------------------------------------------

describe("the analyzer reports every violation, not just the first", () => {
  // THE SURVIVOR THIS EXISTS FOR. A `break` where a `continue` belongs, or a
  // loop that starts at index 1, silently disables a guard while it still
  // reports "found something". Both shapes have shipped in this repo.
  const FIXTURE = [
    ".a{font-size:30px;color:var(--muted)}",
    ".b{font-size:11px;color:var(--faint)}",
    ".c{font-size:10px;color:var(--faint)}",
  ].join("\n");

  it("returns EXACTLY the two offending selectors, in order", () => {
    // RED WHEN: `findFaintColorUses` stops at the first hit (`['.b']`) or skips
    // the first collected item (`['.c']`). Both were applied and both reddened.
    expect(findFaintColorUses(FIXTURE).map((d) => d.selector)).toEqual([".b", ".c"]);
  });

  it("carries the line number and value an operator needs to act", () => {
    // RED WHEN: `line` stops being read from `decl.source.start.line`.
    const hits = findFaintColorUses(FIXTURE);
    expect(hits.map((d) => d.line)).toEqual([2, 3]);
    expect(hits.every((d) => d.value.includes("var(--faint)"))).toBe(true);
  });

  it("the COMPLIANT first item is collected as a color declaration, not skipped", () => {
    // The partner for the assertion above: `['.b','.c']` would also be produced
    // by a walk that never saw `.a` at all.
    // RED WHEN: `findColorDeclarations` filters to `--faint` values itself.
    expect(findColorDeclarations(FIXTURE).map((d) => d.selector)).toEqual([".a", ".b", ".c"]);
  });
});

describe("the parser is not fooled by text that merely LOOKS like a violation", () => {
  it("ignores `var(--faint)` inside a comment, and still sees a real one beside it", () => {
    // A negative with a POSITIVE PARTNER in the same fixture: without the
    // partner, "0 offenders" would also be the answer for a parser that stopped
    // parsing at the comment.
    // RED WHEN: the postcss parse is replaced by a regex over the raw text.
    const css = `.a{/* color: var(--faint) is banned */color:var(--muted)}\n.b{color:var(--faint)}`;
    expect(findFaintColorUses(css).map((d) => d.selector)).toEqual([".b"]);
  });

  it("ignores `var(--faint)` inside a `content:` string, and still sees a real one", () => {
    // RED WHEN: same as above — a regex sees the string's contents.
    const css = `.a{content:"color: var(--faint)";color:var(--muted)}\n.b{color:var(--faint)}`;
    expect(findFaintColorUses(css).map((d) => d.selector)).toEqual([".b"]);
  });

  it("finds a violation NESTED inside an @media block, and records the nesting", () => {
    // The shape a flat scanner reports without context and a naive AST walk
    // that only visits top-level rules misses entirely.
    // RED WHEN: `root.walkDecls` becomes `root.each` over top-level rules only.
    const css = `@media (prefers-color-scheme: dark){\n  .a{color:var(--faint)}\n}`;
    const hits = findFaintColorUses(css);
    expect(hits.map((d) => d.selector)).toEqual([".a"]);
    expect(hits[0].atRules).toEqual(["@media (prefers-color-scheme: dark)"]);
  });

  it("does NOT report `--faint` on border-color, and DOES report it on color", () => {
    // WCAG 1.4.3 is a TEXT rule. A border or a background is judged by 1.4.11
    // against a different threshold, so flagging one here would be a finding
    // this guard cannot justify. The `.b` partner is what stops the negative
    // passing vacuously — without it, a checker that reports NOTHING passes.
    // RED WHEN: the `TEXT_PAINT_PROPS.has(prop)` filter is loosened to
    // `prop.includes("color")`.
    const css = `.a{border-color:var(--faint);background-color:var(--faint)}\n.b{color:var(--faint)}`;
    expect(findFaintColorUses(css).map((d) => d.selector)).toEqual([".b"]);
    expect(findColorDeclarations(css).map((d) => d.selector)).toEqual([".b"]);
  });

  it("DOES report `-webkit-text-fill-color`, which paints the glyphs", () => {
    // THE BYPASS THIS EXISTS FOR, measured in Chromium before it was closed:
    // `-webkit-text-fill-color: var(--faint)` on `.composer-hint` left
    // `getComputedStyle(el).color` reading rgb(107, 104, 98) — 5.27:1, which is
    // what a `color`-only guard scores — while the glyphs actually painted
    // rgb(154, 151, 143) at 2.77:1. Both halves of the guard, plus stylelint,
    // were green on it.
    //
    // It is NOT grouped with border-color/background-color: those paint no
    // glyph and are judged by 1.4.11; this one IS the glyph fill and is
    // squarely 1.4.3.
    // RED WHEN: `-webkit-text-fill-color` is dropped from `TEXT_PAINT_PROPS`.
    const css = `.a{color:var(--muted);-webkit-text-fill-color:var(--faint)}`;
    const hits = findFaintColorUses(css);
    expect(hits.map((d) => d.selector)).toEqual([".a"]);
    expect(hits.map((d) => d.prop)).toEqual(["-webkit-text-fill-color"]);
  });

  it("reports BOTH glyph-painting properties when both carry the token", () => {
    // The partner for the test above: `['.a']` on its own would also be the
    // answer for a sweep that had silently stopped collecting `color`.
    // RED WHEN: `color` is dropped from `TEXT_PAINT_PROPS`.
    const css = `.a{color:var(--faint);-webkit-text-fill-color:var(--faint)}`;
    expect(findFaintColorUses(css).map((d) => d.prop)).toEqual([
      "color",
      "-webkit-text-fill-color",
    ]);
  });

  it("keeps the NARROW `color` population narrow, so the two counters can agree", () => {
    // `findColorDeclarations` feeds the completeness partner, whose independent
    // textual scan counts `color:` alone. If the widening had been applied
    // there instead of in a sibling, the two counters would disagree on valid
    // CSS and red a check that is not about a defect.
    // RED WHEN: `findColorDeclarations` is pointed at `TEXT_PAINT_PROPS`.
    const css = `.a{color:red;-webkit-text-fill-color:blue}`;
    expect(findColorDeclarations(css).map((d) => d.prop)).toEqual(["color"]);
    expect(findTextPaintDeclarations(css).map((d) => d.prop)).toEqual([
      "color",
      "-webkit-text-fill-color",
    ]);
    expect(countColorDeclarationsTextually(css)).toBe(findColorDeclarations(css).length);
  });

  it("reads `VAR(--faint)` — CSS function names are case-INSENSITIVE", () => {
    // THE OTHER FULL BYPASS, measured before it was closed:
    // `.composer-hint { color: VAR(--faint); }` inside the existing
    // `@media (max-width: 600px)` block passed this suite (23 passed), the
    // browser suite (16 passed), `fidelity.spec.ts` (40 passed) and stylelint
    // (exit 0), while every mobile visitor rendered the #396 defect.
    // RED WHEN: `usesFaint` anchors on a literal `var` again.
    const css = `.a{color:VAR(--faint)}\n.b{color:Var( --faint )}`;
    expect(findFaintColorUses(css).map((d) => d.selector)).toEqual([".a", ".b"]);
  });

  it("reads an ESCAPED `var`, and a comment inside it — all four render", () => {
    // THE THIRD FULL BYPASS, and the reason `usesFaint` no longer looks at the
    // function name at all. The first fix matched a literal `var`; the second
    // matched `[vV][aA][rR]`; BOTH were bypassed by a CSS identifier escape.
    // Measured in Chromium — each of these paints #9a978f:
    //     \76 ar(--faint)   \000076ar(--faint)   v\61 r(--faint)   var(/*c*/--faint)
    // Planted as `\76 ar(--faint)` inside the existing
    // `@media (prefers-reduced-motion: reduce)` block, the escape passed this
    // suite (29 passed), stylelint (exit 0) AND the browser suite — which never
    // calls `emulateMedia`, so it cannot reach that block at all. Both halves
    // blind at once: the exact shape #396 exists to prevent, shipped green.
    // RED WHEN: `usesFaint` anchors on the function name in any form instead of
    // on the `--faint` token.
    const css = [
      String.raw`.a{color:\76 ar(--faint)}`,
      String.raw`.b{color:\000076ar(--faint)}`,
      String.raw`.c{color:v\61 r(--faint)}`,
      `.d{color:var(/*c*/--faint)}`,
    ].join("\n");
    expect(findFaintColorUses(css).map((d) => d.selector)).toEqual([".a", ".b", ".c", ".d"]);
  });

  it("finds an ESCAPED `var` in a .tsx inline style too", () => {
    // The source scan had the same blind spot in its own regex.
    // RED WHEN: `findInlineFaintUses` anchors on the function name again.
    const src = [`const a = {`, String.raw`  color: "\76 ar(--faint)",`, `};`].join("\n");
    expect(findInlineFaintUses(src).map((h) => h.line)).toEqual([2]);
  });

  it("does NOT flag a .tsx file that merely MENTIONS --faint in prose", () => {
    // The partner that stops the fix above from becoming a bare `--faint`
    // match. `src/test/buildGuards.test.ts` legitimately names the token in a
    // test title, and flagging that would train the next author to silence
    // this guard. `.b` is the positive partner, so a dead scanner fails too.
    // RED WHEN: `findInlineFaintUses` matches a bare `--faint` with no
    // enclosing call, or stops excluding quotes from the gap.
    const src = [
      `it("selects the #396 --faint contrast guard", () => {`,
      `const note = "stops the --faint token — 2.92:1 at best";`,
      `const b = { color: "var(--faint)" };`,
    ].join("\n");
    expect(findInlineFaintUses(src).map((h) => h.line)).toEqual([3]);
  });

  it("but does NOT report `--FAINT`, because property NAMES are case-sensitive", () => {
    // The paired negative that stops the fix above from being a blanket `i`
    // flag. `--FAINT` is a DIFFERENT custom property (CSS Variables §2); an
    // `i` flag would report it as breaking a rule it does not break. `.b` is
    // the positive partner, so a dead checker cannot pass this either.
    // RED WHEN: `usesFaint` matches `--faint` case-insensitively.
    const css = `.a{color:var(--FAINT)}\n.b{color:var(--faint)}`;
    expect(findFaintColorUses(css).map((d) => d.selector)).toEqual([".b"]);
  });

  it("finds `VAR(--faint)` in a .tsx inline style too", () => {
    // The source scan had the identical bypass, in its own regex.
    // RED WHEN: `findInlineFaintUses`'s regex anchors on a literal `var`.
    const src = [`const a = {`, `  color: "VAR(--faint)",`, `};`].join("\n");
    expect(findInlineFaintUses(src).map((h) => h.line)).toEqual([2]);
  });

  it("reads `var( --faint )` with whitespace, which is valid CSS", () => {
    // RED WHEN: the value test becomes the literal substring `var(--faint)`.
    expect(findFaintColorUses(`.a{color:var( --faint )}`).map((d) => d.selector)).toEqual([".a"]);
  });

  it("does not mistake `--faint-x` for `--faint`", () => {
    // Paired with the line above so this negative cannot pass on a dead checker.
    // RED WHEN: the `(?![\\w-])` lookahead in `usesFaint` becomes `\\b`, which
    // matches before a hyphen and so accepts `--faint-x` (measured: it did).
    const css = `.a{color:var(--faint-x)}\n.b{color:var(--faint)}`;
    expect(findFaintColorUses(css).map((d) => d.selector)).toEqual([".b"]);
  });
});

// ---------------------------------------------------------------------------
// The real files
// ---------------------------------------------------------------------------

describe("no stylesheet in the repo colours text with --faint", () => {
  it("landing.css is clean", () => {
    // RED WHEN: any `color: var(--muted)` in landing.css is put back to
    // `var(--faint)` — the #396 defect itself.
    const hits = findFaintColorUses(landingText);
    expect(
      hits.map((d) => `landing.css:${d.line} ${d.selector} -> ${d.value}`),
      "these rules colour text with a token that reaches 2.92:1 at best in the " +
        "light theme, under even the 3:1 large-text floor",
    ).toEqual([]);
  });

  it("every other stylesheet is clean too, and the sweep really visited them", () => {
    // The enumeration is asserted, not assumed: a sweep over an empty file list
    // reports zero offenders and looks identical to a clean tree.
    // RED WHEN: `CSS_FILES` is narrowed to landing.css alone (the count check
    // reddens), or a `--faint` colour is added to reset.css (the offender
    // check reddens).
    expect(CSS_FILES.length).toBeGreaterThanOrEqual(5);
    const offenders = [];
    for (const f of CSS_FILES) {
      for (const d of findFaintColorUses(readFileSync(f, "utf8"))) {
        offenders.push(`${relative(frontendRoot, f)}:${d.line} ${d.selector}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("no .tsx/.ts source carries an inline var(--faint), which beats every rule", () => {
    // The half the CSS sweep is structurally blind to. Two live inline styles
    // in `Hero.tsx` (the `▸` glyph and the `TRY:` label) survived every earlier
    // `--faint` sweep for exactly this reason.
    // RED WHEN: `color: "var(--muted)"` in Hero.tsx goes back to `var(--faint)`.
    //
    // The enumeration partner names the file the sweep EXISTS for rather than
    // asserting a floor. `>= 20` had 46 files of slack (66 measured), which is
    // the pattern this file's own header condemns (#393): `SOURCE_FILES = []`
    // would have had to lose 66 files before it reddened, and a
    // `walk(...).filter(() => false)` mutant would have passed it. A named
    // member cannot rot that way.
    const relative_ = SOURCE_FILES.map((f) => relative(frontendRoot, f).split("\\").join("/"));
    expect(relative_, "the source sweep no longer visits the file it was built for").toContain(
      "src/components/Hero.tsx",
    );
    const offenders = [];
    for (const f of SOURCE_FILES) {
      for (const h of findInlineFaintUses(readFileSync(f, "utf8"))) {
        offenders.push(`${relative(frontendRoot, f)}:${h.line} ${h.text}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("a .tsx COMMENT may still name the token, so the ban is explainable", () => {
    // Without this, the honest fix — documenting in Hero.tsx why the token was
    // declined — would itself be a violation, and the next author would strip
    // the explanation to get green.
    // RED WHEN: `blankOut(..., { js: true })` is dropped from
    // `findInlineFaintUses`.
    const src = [
      `// never write color: "var(--faint)" here`,
      `/* var(--faint) is banned */`,
      `const a = { color: "var(--muted)" };`,
    ].join("\n");
    expect(findInlineFaintUses(src)).toEqual([]);
  });

  it("...but an inline style IS found, on the line it sits on", () => {
    // The partner for the test above.
    // RED WHEN: `findInlineFaintUses` blanks string literals as well as comments.
    const src = [`const a = {`, `  color: "var(--faint)",`, `};`].join("\n");
    const hits = findInlineFaintUses(src);
    expect(hits.map((h) => h.line)).toEqual([2]);
    expect(hits[0].text).toBe(`color: "var(--faint)",`);
  });

  it("a `//` inside a string does not blank the rest of the line", () => {
    // The one place the comment scanner could hide a real violation.
    // RED WHEN: `blankOut` uses a regex for `//` comments instead of tracking
    // string state.
    const src = `const a = { href: "https://x.example", color: "var(--faint)" };`;
    expect(findInlineFaintUses(src).map((h) => h.line)).toEqual([1]);
  });
});

// ---------------------------------------------------------------------------
// Partner 1: completeness — the parser did not bail out early
// ---------------------------------------------------------------------------

describe("the parser really walked the whole stylesheet", () => {
  it("agrees with an independent textual count of `color:` declarations", () => {
    // Two methods, no shared code path beyond comment/string blanking. A
    // postcss walk that stops after the first `@media` block returns a
    // plausible list and only disagrees with the second opinion.
    // RED WHEN: `findColorDeclarations` returns `found.slice(0, 10)`.
    const parsed = findColorDeclarations(landingText).length;
    const textual = countColorDeclarationsTextually(landingText);
    expect(parsed).toBeGreaterThan(0);
    expect(textual).toBeGreaterThan(0);
    expect(parsed).toBe(textual);
  });

  it("collected EVERY line that is shaped like a `color:` declaration", () => {
    // Stronger than the count: two errors that cancel out keep the totals equal
    // while the parser skips a region. This names the missed lines.
    // RED WHEN: the walk skips declarations inside `@media` blocks.
    const collected = new Set(findColorDeclarations(landingText).map((d) => d.line));
    const missed = colorDeclarationLinesTextually(landingText).filter((l) => !collected.has(l));
    expect(missed, `the parser never visited landing.css lines ${missed.join(", ")}`).toEqual([]);
  });

  it("the two counters disagree the moment the parser is crippled", () => {
    // Proves the two checks above are CAPABLE of failing — they are derived
    // from the file, so nothing else pins them. This is a self-demonstration
    // rather than a guard, and its trigger is honest about that:
    // RED WHEN: landing.css drops to five or fewer `color:` declarations, at
    // which point the equality checks above would agree with a crippled parser
    // and stop being evidence of anything.
    const crippled = findColorDeclarations(landingText).slice(0, 5).length;
    expect(crippled).not.toBe(countColorDeclarationsTextually(landingText));
  });

  it("the textual counter is genuinely INDEPENDENT of postcss", () => {
    // The mutant this exists for: reimplementing the "independent" counter as
    // `findColorDeclarations(cssText).length`. Every check above still passes —
    // the two numbers agree by construction — and the completeness partner
    // becomes a mirror rather than a second opinion.
    //
    // These two inputs make postcss THROW (measured: "Unexpected }" and
    // "Unclosed block"), so a delegating implementation cannot answer them at
    // all, while a textual scan can.
    // RED WHEN: `countColorDeclarationsTextually` delegates to postcss.
    expect(() => findColorDeclarations(".a{color:red}}")).toThrow();
    expect(countColorDeclarationsTextually(".a{color:red}}")).toBe(1);
    expect(() => findColorDeclarations(".a{color:red")).toThrow();
    expect(countColorDeclarationsTextually(".a{color:red")).toBe(1);
  });

  it("both counters read `COLOR:` the way CSS does — case-insensitively", () => {
    // CSS property names are case-insensitive; a JS regex is not. A
    // case-sensitive textual scan would have disagreed with postcss on valid
    // CSS and reddened the completeness check for a reason that is not a defect.
    // RED WHEN: the `i` flag is dropped from the textual scanner's regex.
    const css = `.a{COLOR:red}\n.b{color:blue}`;
    expect(findColorDeclarations(css).length).toBe(2);
    expect(countColorDeclarationsTextually(css)).toBe(2);
    expect(colorDeclarationLinesTextually(css)).toEqual([1, 2]);
  });

  it("blankOut preserves line numbering exactly", () => {
    // Everything above reports line numbers through it.
    // RED WHEN: `blankOut` collapses a comment to a single space.
    const src = "a\n/* two\nlines */\nb";
    expect(blankOut(src).split("\n").length).toBe(src.split("\n").length);
    expect(blankOut(src).length).toBe(src.length);
  });
});

// ---------------------------------------------------------------------------
// Partner 2: the detector still detects
// ---------------------------------------------------------------------------

describe("the analyzer has not gone blind on the real stylesheet", () => {
  it("reports many real selectors once --muted is swapped back to --faint", () => {
    // Derived from the file, never a magic constant: the expected count IS the
    // number of `color: var(--muted)` declarations the file has today, read
    // from the file. A `>= 20` floor would rot the way #393's did.
    // RED WHEN: `findFaintColorUses` is stubbed to return `[]`.
    // `findTextPaintDeclarations`, NOT `findColorDeclarations`: the swap below
    // is measured against `findFaintColorUses`, which sweeps every
    // glyph-painting property. Taking `before` from the NARROW `color`-only
    // population made these two sides count different things — agreeing today
    // only because nothing in the repo sets `-webkit-text-fill-color`, and
    // reddening for a non-defect the first time something legitimately did.
    const before = findTextPaintDeclarations(landingText).filter((d) =>
      /var\(\s*--muted\b/.test(d.value),
    );
    expect(before.length).toBeGreaterThan(0);

    const mutated = landingText.replaceAll("var(--muted)", "var(--faint)");
    const hits = findFaintColorUses(mutated);
    expect(hits.length).toBe(before.length);

    // Real selectors, not placeholders — this is what makes the report usable.
    //
    // DERIVED, not a hardcoded shortlist. An earlier draft named
    // `.composer-hint`, `.mono-label`, `.step-number` and `.footer-copy`
    // literally, and that made this test fail FOR THE WRONG REASON: when the
    // `VAR(--faint)` bypass was planted, the only static check that reddened
    // was this one, and it reddened because `.composer-hint` had stopped
    // saying `var(--muted)` — not because a violation had been detected. The
    // primary check (`landing.css is clean`) stayed green. Comparing the two
    // sets keeps this a self-demonstration of the analyzer rather than a
    // second, brittle assertion about the stylesheet's contents.
    expect(new Set(hits.map((h) => h.selector))).toEqual(new Set(before.map((d) => d.selector)));
    // Non-vacuity for the set equality: two empty sets are equal.
    expect(new Set(before.map((d) => d.selector)).size).toBeGreaterThan(10);
  });

  it("and the same swap in a .tsx source is found inline", () => {
    // The browser-invisible half of the same partner.
    // RED WHEN: `findInlineFaintUses` stops scanning string literals.
    const hero = readFileSync(join(frontendRoot, "src", "components", "Hero.tsx"), "utf8");
    expect(hero).toContain(`color: "var(--muted)"`);
    const mutated = hero.replaceAll(`color: "var(--muted)"`, `color: "var(--faint)"`);
    expect(findInlineFaintUses(mutated).length).toBeGreaterThanOrEqual(2);
  });
});
