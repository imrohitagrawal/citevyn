/**
 * #403 — `tokens.css` declares the dark theme TWICE, and a one-place edit is a
 * bug.
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
 *   - the three `data-style` skins further down the file. Nothing sets
 *     `data-style` (tokens.css says so at length), so they render for nobody.
 *   - a token declared in `:root` and overridden in only ONE dark block —
 *     impossible by construction here, since that is precisely what "the two
 *     blocks declare the same set" forbids.
 *
 * ONE LINE PER TEST names the change that turns it red.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import postcss, { Rule, Declaration, AtRule } from "postcss";

const TOKENS_CSS = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "styles",
  "tokens.css",
);

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
function darkBlocks(css: string): { toggle: Rule | null; osPreference: Rule | null } {
  const root = postcss.parse(css, { from: TOKENS_CSS });
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
    if (inDarkMedia && selector === ':root:not([data-theme])') {
      osPreference = rule;
    } else if (!inDarkMedia && selector === '[data-theme="dark"]') {
      toggle = rule;
    }
  });
  return { toggle, osPreference };
}

describe("tokens.css: the two dark blocks are one theme, not two", () => {
  const css = readFileSync(TOKENS_CSS, "utf8");
  const { toggle, osPreference } = darkBlocks(css);

  it("finds both dark blocks, each with real declarations", () => {
    // The partner for every assertion below. "The two maps are equal" is
    // vacuously true when both are empty, which is exactly what a renamed
    // selector or a broken parse produces.
    // RED WHEN: either selector is renamed, or the media query's condition
    // changes so `darkBlocks` no longer matches it.
    expect(toggle, '[data-theme="dark"] was not found in tokens.css').not.toBeNull();
    expect(
      osPreference,
      "@media (prefers-color-scheme: dark) > :root:not([data-theme]) was not found",
    ).not.toBeNull();
    expect(customProperties(toggle!).length).toBeGreaterThan(30);
    expect(customProperties(osPreference!).length).toBeGreaterThan(30);
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

  it("the four #403 semantic colours really do differ from the light values", () => {
    // Without this, the whole file passes on a tokens.css whose dark blocks are
    // an exact copy of the light values — which is the state that SHIPPED the
    // seven AA failures, and which every assertion above is perfectly happy
    // with. This is the partner that makes "in parity" mean something.
    // RED WHEN: any of these four is reverted to its light value in the dark
    // blocks (equivalently: the theme split is undone).
    const root = postcss.parse(css, { from: TOKENS_CSS });
    let light: Rule | null = null;
    root.walkRules((rule: Rule) => {
      if (rule.parent?.type === "root" && rule.selector.replace(/\s+/g, " ").trim() ===
          ':root, [data-theme="light"]') {
        light = rule;
      }
    });
    expect(light, "the light :root block was not found").not.toBeNull();
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

  it("the comparator can still SEE a drifted block", () => {
    // The detector-still-works partner. Four green "no differences" results
    // above are indistinguishable from a comparator that returns [] whatever it
    // is given, so this plants each drift shape in a COPY of the real
    // stylesheet and requires it to be reported.
    // RED WHEN: `darkBlocks` stops matching a selector, `customProperties`
    // stops collecting, or either comparison is weakened to a no-op.
    const diff = (text: string) => {
      const { toggle: t, osPreference: o } = darkBlocks(text);
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

    // 1. A token retuned in the toggle block only. The replacement is applied
    //    to the FIRST occurrence after the `[data-theme="dark"] {` marker, so
    //    it cannot accidentally hit the light block or the media block.
    const markerAt = css.indexOf('[data-theme="dark"] {');
    expect(markerAt, "the [data-theme=dark] block marker moved").toBeGreaterThan(0);
    const head = css.slice(0, markerAt);
    const tail = css.slice(markerAt);
    const retunedAt = tail.indexOf("--color-warning: #e0a458;");
    expect(retunedAt, "the dark --color-warning declaration moved").toBeGreaterThan(0);
    const retuned =
      head +
      tail.slice(0, retunedAt) +
      "--color-warning: #123456;" +
      tail.slice(retunedAt + "--color-warning: #e0a458;".length);
    expect(diff(retuned), "a value that drifted in one block was not reported").toContain(
      "differs:--color-warning",
    );

    // 2. A token DROPPED from the OS-preference block — the exact shape that
    //    shipped, and the one a value comparison alone cannot see.
    const osAt = css.indexOf(":root:not([data-theme])");
    expect(osAt, "the OS-preference selector moved").toBeGreaterThan(0);
    const osHead = css.slice(0, osAt);
    const osTail = css.slice(osAt);
    const dropAt = osTail.indexOf("--color-error-strong: #ef9a89;");
    expect(dropAt, "the OS-preference --color-error-strong declaration moved").toBeGreaterThan(0);
    const dropped =
      osHead +
      osTail.slice(0, dropAt) +
      osTail.slice(dropAt + "--color-error-strong: #ef9a89;".length);
    expect(diff(dropped), "a token missing from the OS-preference block was not reported").toContain(
      "only-toggle:--color-error-strong",
    );
  });
});
