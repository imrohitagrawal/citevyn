/**
 * #403 — the dark theme is declared TWICE in every themed stylesheet here, and
 * a one-place edit is a bug.
 *
 * The two blocks are:
 *
 *   [data-theme="dark"]                                  — the manual toggle
 *   @media (prefers-color-scheme: dark) {
 *     :root:not([data-theme])                            — the OS preference
 *   }
 *
 * The second one is what the DEFAULT visitor gets: nothing writes `data-theme`
 * onto <html> until someone clicks the theme toggle, so "system theme, OS set
 * to dark" — the common case — is served entirely by the media block. Before
 * this guard, that block stopped after the border tokens: it carried no
 * semantic colours, no window chrome, and no highlighter tokens at all. Those
 * fell through to the LIGHT `:root` values.
 *
 * That was invisible for exactly as long as the missing values happened to be
 * identical in both themes. #403 splits four semantic colours by theme, which
 * turns the omission into six wrong colours for the most common dark-mode
 * reader — so the duplication needs a mechanical check rather than a comment
 * asking the next editor to remember.
 *
 * WHAT THIS CANNOT SEE, stated because a guard that reads as exhaustive and is
 * not is worse than no guard:
 *   - whether the values are the RIGHT colours. That is
 *     `frontend/tests/contrast-floor.spec.ts`, which measures the rendered
 *     page. This file only asserts the two blocks agree with each other.
 *   - the three `data-style` skins further down `tokens.css`. Nothing sets
 *     `data-style` (that file says so at length), so they render for nobody.
 *   - a NEW stylesheet that adopts the same two-block shape. `THEMED_STYLESHEETS`
 *     below is a hand-kept list, which is this guard's one weakness; it is
 *     written down rather than left implied.
 *   - A TOKEN DECLARED ONLY IN `:root`. An earlier draft of this line claimed
 *     the shape was "impossible by construction", and an adversarial review
 *     disproved it: that is true only of a token declared in `:root` AND in one
 *     dark block. Declare a new theme-sensitive token in `:root` and in NEITHER
 *     dark block, and this comparator reports nothing while every dark reader
 *     gets the light value — the same class of defect #403 fixed, mirrored. Two
 *     things narrow it: a token that paints TEXT is caught by
 *     `frontend/tests/contrast-floor.spec.ts` in both themes, and the
 *     `describe` at the bottom of this file pins the four #403 colours by name.
 *     Neither closes it in general.
 *   - WHETHER AN ALIAS IS STILL AN ALIAS. `--refusal-amber: var(--color-warning)`
 *     could be frozen back to a literal and nothing here would notice — and
 *     nothing else would either, because its only consumer
 *     (`Hero.tsx`'s inline `borderColor`) is overridden for its whole lifetime
 *     by `landing.css`'s `.hero-input-box.shake { … !important }`, which is
 *     driven by the SAME state flag. Recorded in `tokens.css` beside the alias.
 *
 * ONE LINE PER TEST names the change that turns it red.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import postcss, { Rule, Declaration, AtRule } from "postcss";

const HERE = dirname(fileURLToPath(import.meta.url));
const TOKENS_CSS = join(HERE, "..", "styles", "tokens.css");

/**
 * EVERY stylesheet in this repo that declares the dark theme twice.
 *
 * `tokens.css` is the SPA's. `public/about.css` is the second one, found by
 * grepping for the shape rather than assumed to be absent: `/about` is a static
 * page outside the SPA with its own copy of the tokens it needs, and it carries
 * the identical two-block structure with a slightly different OS-preference
 * selector (`:root:not([data-theme="light"])` rather than
 * `:root:not([data-theme])`). It declares no semantic colours, so #403's split
 * does not reach it — which is exactly why it would have been the next place
 * this defect appeared unchecked.
 *
 * `reset.css` and `fonts.css` declare no theme block at all, and `landing.css`
 * consumes tokens without redeclaring them, so neither can drift this way.
 * NOT claimed to be exhaustive for files added later — a new stylesheet with
 * two dark blocks would have to be added here by hand, which is this list's one
 * weakness and is written down rather than left implied.
 */
const THEMED_STYLESHEETS: { label: string; path: string }[] = [
  { label: "src/styles/tokens.css", path: TOKENS_CSS },
  { label: "public/about.css", path: join(HERE, "..", "..", "public", "about.css") },
];

/** Every custom-property declaration in a rule, in source order. */
function customProperties(rule: Rule): [string, string][] {
  const out: [string, string][] = [];
  rule.walkDecls((decl: Declaration) => {
    if (decl.prop.startsWith("--")) out.push([decl.prop, decl.value.trim()]);
  });
  return out;
}

/**
 * The two dark blocks, found by walking the real PostCSS tree.
 *
 * Selector matching is normalized on whitespace only — not on a hand-written
 * regex over the file text, which cannot tell a selector from the same
 * characters inside a comment, and this file's comments quote both selectors
 * verbatim several times.
 */
function darkBlocks(css: string, from: string): { toggle: Rule | null; osPreference: Rule | null } {
  const root = postcss.parse(css, { from });
  let toggle: Rule | null = null;
  let osPreference: Rule | null = null;
  const norm = (s: string) => s.replace(/\s+/g, " ").trim();

  root.walkRules((rule: Rule) => {
    const selector = norm(rule.selector);
    const parent = rule.parent;
    const inDarkMedia =
      parent?.type === "atrule" &&
      (parent as AtRule).name === "media" &&
      /prefers-color-scheme\s*:\s*dark/.test((parent as AtRule).params);
    // The OS-preference rule is matched by its SHAPE (a `:root` rule inside the
    // dark media query), not by one exact string: the two stylesheets spell its
    // `:not(...)` differently, and pinning one spelling would silently find
    // nothing in the other and pass vacuously.
    if (inDarkMedia && selector.startsWith(":root")) {
      osPreference = rule;
    } else if (!inDarkMedia && selector === '[data-theme="dark"]') {
      toggle = rule;
    }
  });
  return { toggle, osPreference };
}

for (const { label, path } of THEMED_STYLESHEETS) {
describe(`${label}: the two dark blocks are one theme, not two`, () => {
  const css = readFileSync(path, "utf8");
  const { toggle, osPreference } = darkBlocks(css, path);

  it("finds both dark blocks, each with real declarations", () => {
    // The partner for every assertion below. "The two maps are equal" is
    // vacuously true when both are empty, which is exactly what a renamed
    // selector or a broken parse produces.
    //
    // The floor is ZERO-PLUS, not a tuned number. `about.css` carries six
    // tokens in each dark block and `tokens.css` carries far more, so any
    // threshold picked for one is arbitrary for the other — and a threshold
    // near the smaller file's size turns a dropped token into a failure HERE
    // rather than in the equality test that names it. What this must assert is
    // only "these blocks are not empty", because that is the one state the
    // equality below is vacuously happy with.
    // RED WHEN: either selector is renamed, or the media query's condition
    // changes so `darkBlocks` no longer matches it.
    expect(toggle, `[data-theme="dark"] was not found in ${label}`).not.toBeNull();
    expect(
      osPreference,
      `@media (prefers-color-scheme: dark) > :root… was not found in ${label}`,
    ).not.toBeNull();
    expect(customProperties(toggle!).length, `${label}'s toggle block is empty`).toBeGreaterThan(0);
    expect(
      customProperties(osPreference!).length,
      `${label}'s OS-preference block is empty`,
    ).toBeGreaterThan(0);
  });

  it("declares the SAME tokens, with the SAME values, in both", () => {
    // RED WHEN: a token is added to, removed from, or retuned in one dark block
    // without the other — the shape that left the OS-preference block with no
    // semantic colours at all until #403.
    const a = new Map(customProperties(toggle!));
    const b = new Map(customProperties(osPreference!));

    const onlyToggle = [...a.keys()].filter((k) => !b.has(k));
    const onlyOs = [...b.keys()].filter((k) => !a.has(k));
    const differing = [...a.entries()]
      .filter(([k, v]) => b.has(k) && b.get(k) !== v)
      .map(([k, v]) => `${k}: ${v} (toggle) vs ${b.get(k)} (OS preference)`);

    expect(
      onlyToggle,
      'declared for [data-theme="dark"] but NOT for the OS preference — the default ' +
        "dark-mode reader gets the light value for these",
    ).toEqual([]);
    expect(onlyOs, 'declared for the OS preference but NOT for [data-theme="dark"]').toEqual([]);
    expect(differing, "the same token has two different dark values").toEqual([]);
  });

  it("declares each token at most ONCE per block", () => {
    // `--grid-color` was written twice in the OS-preference block. Harmless
    // while both copies agreed; a silent last-one-wins the moment they did not.
    // The map comparison above cannot see it — a Map keeps only the last value.
    // RED WHEN: any property is repeated inside either dark block.
    for (const [label, rule] of [
      ['[data-theme="dark"]', toggle!],
      ["OS preference", osPreference!],
    ] as const) {
      const props = customProperties(rule).map(([p]) => p);
      const seen = new Set<string>();
      const dupes = props.filter((p) => (seen.has(p) ? true : (seen.add(p), false)));
      expect(dupes, `${label} declares these more than once`).toEqual([]);
    }
  });

  it("the comparator can still SEE a drifted block", () => {
    // The detector-still-works partner. Three green "no differences" results
    // above are indistinguishable from a comparator that returns [] whatever it
    // is given, so this plants each drift shape in a COPY of the real
    // stylesheet and requires it to be reported.
    //
    // The plants are DERIVED from the parsed file (the first custom property in
    // each block, edited inside that block's own source range) rather than
    // written as literals. A literal would have to be a different string per
    // stylesheet, and a literal that stopped matching would make this test pass
    // on a plant that was never applied — the shape that produces a false
    // survival.
    // RED WHEN: `darkBlocks` stops matching a selector, `customProperties`
    // stops collecting, or either comparison is weakened to a no-op.
    const diff = (text: string) => {
      const { toggle: t, osPreference: o } = darkBlocks(text, path);
      if (!t || !o) return ["<a block went missing>"];
      const a = new Map(customProperties(t));
      const b = new Map(customProperties(o));
      return [
        ...[...a.keys()].filter((k) => !b.has(k)).map((k) => `only-toggle:${k}`),
        ...[...b.keys()].filter((k) => !a.has(k)).map((k) => `only-os:${k}`),
        ...[...a.entries()]
          .filter(([k, v]) => b.has(k) && b.get(k) !== v)
          .map(([k]) => `differs:${k}`),
      ];
    };

    // Precondition: the real file is clean, so anything the plants report is
    // the plant and not pre-existing drift.
    expect(diff(css)).toEqual([]);

    /** Rewrite one declaration inside ONE rule's source range. */
    const editInRule = (rule: Rule, prop: string, replacement: string) => {
      const from = rule.source!.start!.offset;
      const to = rule.source!.end!.offset;
      const region = css.slice(from, to);
      const decl = new RegExp(`${prop.replace(/[-]/g, "\\-")}\\s*:[^;]*;`).exec(region);
      // The plant is confirmed to have LANDED. A regex that matched nothing
      // would leave the text unchanged, and "not reported" would then be
      // correct behaviour reported as a pass.
      expect(decl, `could not find ${prop} inside ${rule.selector}`).not.toBeNull();
      const edited = region.slice(0, decl!.index) + replacement + region.slice(decl!.index + decl![0].length);
      expect(edited, "the plant changed nothing").not.toBe(region);
      return css.slice(0, from) + edited + css.slice(to);
    };

    // 1. A token retuned in the toggle block only.
    const [firstProp] = customProperties(toggle!)[0];
    expect(
      diff(editInRule(toggle!, firstProp, `${firstProp}: #123456;`)),
      "a value that drifted in one block was not reported",
    ).toContain(`differs:${firstProp}`);

    // 2. A token DROPPED from the OS-preference block — the exact shape that
    //    shipped, and the one a value comparison alone cannot see.
    const [osProp] = customProperties(osPreference!)[0];
    expect(
      diff(editInRule(osPreference!, osProp, "")),
      "a token missing from the OS-preference block was not reported",
    ).toContain(`only-toggle:${osProp}`);
  });
});
}

describe("tokens.css: the #403 theme split is real, not a copy of the light values", () => {
  // Scoped to `tokens.css` alone, and deliberately outside the per-file loop
  // above: `public/about.css` declares no semantic colours at all (it carries
  // only surfaces, ink, muted, border and hl), so requiring these four there
  // would assert something false about a file this change does not touch.
  const css = readFileSync(TOKENS_CSS, "utf8");
  const { toggle } = darkBlocks(css, TOKENS_CSS);

  it("the four split semantic colours differ from their light values", () => {
    // Without this, the parity file passes on a tokens.css whose dark blocks
    // are an exact copy of the light values — which is the state that SHIPPED
    // the seven AA failures, and which every parity assertion is perfectly
    // happy with. This is the partner that makes "in parity" mean something.
    // RED WHEN: any of these four is reverted to its light value in the dark
    // blocks (equivalently: the theme split is undone).
    const root = postcss.parse(css, { from: TOKENS_CSS });
    let light: Rule | null = null;
    root.walkRules((rule: Rule) => {
      if (
        rule.parent?.type === "root" &&
        rule.selector.replace(/\s+/g, " ").trim() === ':root, [data-theme="light"]'
      ) {
        light = rule;
      }
    });
    expect(light, "the light :root block was not found").not.toBeNull();
    expect(toggle, 'the [data-theme="dark"] block was not found').not.toBeNull();
    const lightMap = new Map(customProperties(light!));
    const darkMap = new Map(customProperties(toggle!));

    for (const token of [
      "--color-success",
      "--color-warning",
      "--color-error",
      "--color-error-strong",
    ]) {
      expect(lightMap.has(token), `${token} is not declared in the light block`).toBe(true);
      expect(darkMap.has(token), `${token} is not declared in the dark block`).toBe(true);
      expect(
        darkMap.get(token),
        `${token} has the SAME value in both themes — #403 split it because one ` +
          "value cannot clear 4.5:1 on both #ffffff and #1e1e21",
      ).not.toBe(lightMap.get(token));
    }
  });
});
