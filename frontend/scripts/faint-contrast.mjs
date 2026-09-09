/**
 * #396 — the static half of the `--faint` guard.
 *
 * THE RULE, AND WHY IT HAS NO FONT-SIZE CONDITION
 * -----------------------------------------------
 * `color: var(--faint)` is banned for text anywhere in `frontend/src`. Not
 * "banned below 24px", not "banned unless bold" — banned, full stop.
 *
 * That is not severity inflation, it is what the measurement says. WCAG 1.4.3
 * gives large text (>= 24px, or >= 18.667px at weight >= 700) a relaxed 3:1
 * floor. `--faint`'s BEST light-theme ratio against any surface it actually
 * paints on is 2.92:1 (on `--surface` #ffffff; 2.77 on `--bg`, 2.58 on
 * `--surface-2`), which is under even that relaxed floor. So there is no size
 * and no weight at which the token is legal for text in the light theme, and a
 * size-conditional rule would only add a way to be wrong.
 *
 * That also removes this guard's one plausible blind spot. A rule keyed on
 * `font-size` cannot see an INHERITED size: `.chat-input::placeholder` and
 * `.generic-avatar` set no `font-size` of their own (they inherit 16px and
 * 12px from `.chat-input` and `.compare-avatar`), so a size-aware checker
 * would have had to resolve the cascade to judge them — statically, it cannot.
 * With no size condition there is nothing to resolve.
 *
 * WHY POSTCSS AND NOT A REGEX
 * ---------------------------
 * A regex over the stylesheet cannot tell a declaration from the same text
 * inside a `/* comment *\/` or inside a `content: "..."` string, and it has no
 * idea whether a rule sits inside an `@media` block. All three shapes are
 * fixtures in the test file next to this one. `postcss` parses the real
 * grammar, so none of them can fool it.
 *
 * WHY `postcss` IS NOW AN EXPLICIT devDependency. It was already installed
 * transitively (8.5.26, via stylelint and vite); naming it in
 * `frontend/package.json` means a future upstream extras change cannot
 * silently break this test. Same precedent as `pyyaml` in
 * `backend/pyproject.toml`.
 *
 * WHAT THIS FILE CANNOT SEE. Stylesheets and source text only. An inline
 * `style={{ color: "var(--faint)" }}` in a `.tsx` file beats every stylesheet
 * rule and is invisible to the CSS half — which is exactly how two live uses in
 * `Hero.tsx` survived every earlier sweep — so `findInlineFaintUses` exists for
 * the TSX side, and `frontend/tests/faint-contrast.spec.ts` asserts what the
 * browser actually renders. Neither half alone is the guard.
 *
 * Pure and importable: nothing here runs at import time.
 */
import postcss from "postcss";

/**
 * Blank out comments — and optionally string literals — while preserving every
 * byte offset and every newline, so line numbers reported against the result
 * are line numbers in the original.
 *
 * A character scanner rather than a regex because the two constructs nest: `/*`
 * inside a string is not a comment, and a quote inside a comment does not open
 * a string. `js: true` additionally recognises `//` line comments and template
 * literals, which CSS does not have.
 */
export function blankOut(text, { js = false, strings = false } = {}) {
  const out = text.split("");
  let i = 0;
  const n = text.length;
  const blank = (from, to) => {
    for (let k = from; k < to && k < n; k++) if (out[k] !== "\n") out[k] = " ";
  };
  while (i < n) {
    const c = text[i];
    const next = text[i + 1];
    if (c === "/" && next === "*") {
      let j = text.indexOf("*/", i + 2);
      j = j === -1 ? n : j + 2;
      blank(i, j);
      i = j;
      continue;
    }
    if (js && c === "/" && next === "/") {
      let j = text.indexOf("\n", i + 2);
      j = j === -1 ? n : j;
      blank(i, j);
      i = j;
      continue;
    }
    if (c === '"' || c === "'" || (js && c === "`")) {
      let j = i + 1;
      while (j < n) {
        if (text[j] === "\\") {
          j += 2;
          continue;
        }
        if (text[j] === c) {
          j += 1;
          break;
        }
        // An unterminated CSS string ends at the newline (CSS 2.1 §4.3.4).
        if (!js && text[j] === "\n") break;
        j += 1;
      }
      if (strings) blank(i + 1, j - 1 >= i + 1 ? j - 1 : i + 1);
      i = j;
      continue;
    }
    i += 1;
  }
  return out.join("");
}

/** The `@`-rule context a node sits in, outermost first (e.g. `@media (min-width: 700px)`). */
function atRuleChain(node) {
  const chain = [];
  for (let p = node.parent; p; p = p.parent) {
    if (p.type === "atrule") chain.unshift(`@${p.name} ${p.params}`.trim());
  }
  return chain;
}

/** The selector a declaration belongs to, or the `@`-rule when it has no rule parent. */
function ownerSelector(node) {
  const p = node.parent;
  if (!p) return "<root>";
  if (p.type === "rule") return p.selector.replace(/\s+/g, " ").trim();
  if (p.type === "atrule") return `@${p.name} ${p.params}`.trim();
  return "<root>";
}

/**
 * EVERY `color:` declaration in `cssText`, structurally.
 *
 * `color` EXACTLY. This function is deliberately the NARROW one: it is the
 * population the completeness partner in the test file compares against an
 * independent textual `color:` scan, and that scan's `(?:^|[;{}])` boundary
 * counts `color` only. Widening this would make the two counters disagree on
 * valid CSS and red a check that is not about a defect.
 *
 * The VIOLATION sweep is `findTextPaintDeclarations` below, which is wider.
 *
 * Returns `{ selector, line, value, atRules }` per declaration, in source order.
 */
export function findColorDeclarations(cssText) {
  return declarationsWhere(cssText, (prop) => prop === "color");
}

/**
 * Properties that PAINT GLYPHS, and so fall under WCAG 1.4.3.
 *
 * `-webkit-text-fill-color` is here because it is not a synonym for `color` —
 * it OVERRIDES it for the glyph fill while `getComputedStyle(el).color` keeps
 * reporting the old value. Measured in Chromium on `.composer-hint`:
 * `color` read `rgb(107, 104, 98)` (5.27:1, which is what a `color`-only guard
 * scores) while `-webkit-text-fill-color` read `rgb(154, 151, 143)`, so the
 * text actually painted at 2.77:1 — the exact #396 defect, invisible to both
 * halves of the guard until this list existed.
 *
 * `border-color` and `background-color` are deliberately NOT here, and the
 * reason is different from the one an earlier draft of this comment gave. They
 * are excluded because they paint no glyph: a border or a background is judged
 * by WCAG 1.4.11 (non-text contrast, 3:1) against a different threshold, so
 * reporting them under a 4.5:1 TEXT rule would be a finding this guard cannot
 * justify. That reasoning never applied to `-webkit-text-fill-color`, which
 * paints the glyphs themselves and is squarely 1.4.3.
 *
 * WHAT IS STILL NOT COVERED, stated rather than implied: `-webkit-text-stroke-color`
 * (paints an outline, not a fill — a stroke alone cannot make text legible, and
 * nothing in this repo sets one), and any future unprefixed `text-fill-color`,
 * which no browser ships today. Both are one entry away if that changes.
 */
const TEXT_PAINT_PROPS = new Set(["color", "-webkit-text-fill-color"]);

/** Shared postcss walk; `keep` receives the LOWERCASED property name. */
function declarationsWhere(cssText, keep) {
  const root = postcss.parse(cssText);
  const found = [];
  root.walkDecls((decl) => {
    const prop = decl.prop.toLowerCase();
    if (!keep(prop)) return;
    found.push({
      selector: ownerSelector(decl),
      prop,
      line: decl.source?.start?.line ?? 0,
      value: decl.value.trim(),
      atRules: atRuleChain(decl),
    });
  });
  return found;
}

/**
 * Every declaration that paints GLYPHS — `color` and `-webkit-text-fill-color`.
 * Same shape as `findColorDeclarations`, plus the `prop` that was matched, so a
 * report can say WHICH property carried the token.
 */
export function findTextPaintDeclarations(cssText) {
  return declarationsWhere(cssText, (prop) => TEXT_PAINT_PROPS.has(prop));
}

/**
 * True when a declaration value reads the `--faint` custom property.
 *
 * MATCHES THE TOKEN, NEVER THE SPELLING OF `var`. Two earlier versions of this
 * function tried to match the function name and each was bypassed by a spelling
 * they had not thought of — first `VAR(--faint)` (function names are ASCII
 * case-insensitive), then, once that was fixed with `[vV][aA][rR]`, a CSS
 * IDENTIFIER ESCAPE. All four of these render `--faint` in Chromium, measured:
 *
 *     VAR(--faint)        \76 ar(--faint)      v\61 r(--faint)     var(/*…* /--faint)
 *
 * There is no legal way for the sequence `--faint` to appear in a `color` or
 * `-webkit-text-fill-color` value EXCEPT as a read of that custom property, so
 * matching the token covers every present and future spelling of the function
 * at once, with no false-positive surface. Verified against all five shipped
 * stylesheets: zero hits before the #396 sweep is undone, and the `--faint`
 * mentions in this repo's CSS COMMENTS are not part of any `decl.value`.
 *
 * The negative lookahead keeps two things out that are genuinely not this
 * rule's business: `--faint-x` (a different property), and `--FAINT` — custom
 * property NAMES are case-SENSITIVE (CSS Variables §2), so an `i` flag here
 * would report a violation of a rule that is not broken. Confirmed in a real
 * browser: `--FAINT` resolves to a different value, not to `--faint`.
 */
function usesFaint(value) {
  return /--faint(?![\w-])/.test(value);
}

/**
 * The violations: glyph-painting declarations whose value reads `var(--faint)`.
 * Same shape as `findTextPaintDeclarations`, so a caller can report file, line
 * and property.
 */
export function findFaintColorUses(cssText) {
  return findTextPaintDeclarations(cssText).filter((d) => usesFaint(d.value));
}

/**
 * `var(--faint)` in a TSX/TS/JS source — the inline-style half.
 *
 * Comments are blanked first (a `.tsx` file is allowed to EXPLAIN why the token
 * is banned; `Hero.tsx` now does exactly that), string literals are not — an
 * inline style IS a string literal, so blanking those would blank the subject.
 *
 * Deliberately NOT restricted to a `color:` key. In JSX the property is
 * `color`, `WebkitTextFillColor`, a spread, a variable, or a `clsx` result;
 * there is no reliable static shape. Since the token has no legal text use at
 * all, "any mention of `var(--faint)` outside a comment" is both simpler and
 * strictly harder to slip past.
 *
 * COST OF THAT CHOICE, paid once already. A string literal that merely QUOTES
 * the token — a test's failure message, say — is reported too. That happened
 * while writing this: `src/test/buildGuards.test.ts`'s message named the token
 * with its wrapper and became a violation of the rule it protects. The remedy
 * is to name the token without the wrapper, or to put the mention in a comment.
 * Kept deliberately: telling a quoted mention from a live inline style needs a
 * real JS parser, and of the two ways to be wrong, reporting too much is the
 * one that cannot hide a defect.
 *
 * KNOWN IMPRECISION, and which way it fails. `blankOut` is a character scanner,
 * not a JS tokenizer, so an apostrophe in JSX TEXT (`Who it's for`) reads as a
 * string opener and the scanner skips to the next apostrophe. Skipping BLANKS
 * NOTHING — `strings` is false here — so the worst case is that a `//` or
 * `/* *\/` comment inside that span keeps its text and gets REPORTED. That is a
 * false positive on a comment, which fails closed and is visible; it cannot
 * hide a real inline style.
 */
export function findInlineFaintUses(sourceText) {
  const scrubbed = blankOut(sourceText, { js: true });
  const hits = [];
  // Spelling-agnostic, for the reason `usesFaint` gives: `VAR(`, `\76 ar(` and
  // `var(/*c*/` all render. So this anchors on "`--faint` inside SOME function
  // call" rather than on the name of the function.
  //
  // Unlike the CSS side, a bare `--faint` match would be wrong here: TS/TSX
  // source legitimately MENTIONS the token in test titles and messages (e.g.
  // `it("selects the #396 --faint contrast guard")` in src/test/), and flagging
  // those would train a future author to silence this guard. Quote characters
  // are excluded from the gap so a prose mention that merely follows some
  // unrelated `(` cannot match, while every real inline style — where the
  // token sits inside a `var()` inside one string — still does.
  const re = /\([^)"'`]{0,40}--faint(?![\w-])[^)]*\)/g;
  let m;
  while ((m = re.exec(scrubbed)) !== null) {
    const line = scrubbed.slice(0, m.index).split("\n").length;
    hits.push({
      line,
      match: m[0],
      // The original line, not the scrubbed one, so a report is readable.
      text: sourceText.split("\n")[line - 1]?.trim() ?? "",
    });
  }
  return hits;
}

/**
 * A SECOND, INDEPENDENT count of `color:` declarations, by a different method:
 * blank the comments AND the string literals, then scan for the declaration
 * shape textually. It shares no code path with postcss beyond `blankOut`.
 *
 * Its job is to make "the parser found zero violations" distinguishable from
 * "the parser found nothing at all". A completeness partner that re-used
 * postcss would agree with a broken postcss walk by construction.
 *
 * The `(?:^|[;{}])` boundary is what keeps `border-color`, `background-color`
 * AND `-webkit-text-fill-color` out: in all three, `color` is preceded by `-`.
 * That is deliberate here — this counter's only job is to be a second opinion
 * on `findColorDeclarations`, which is narrow for the same reason. The WIDER
 * violation sweep is `findTextPaintDeclarations`, which has no textual partner
 * and does not need one: it is not the population any count is derived from.
 *
 * Case-insensitive to match postcss, which lowercases nothing but whose
 * consumer here does: `COLOR: red` is one declaration to both, and a
 * case-sensitive scan here would have made the two counters disagree on valid
 * CSS (#322's lesson — a dict is case-sensitive, CSS property names are not.
 * An earlier draft cited #365, which is the render-blocking font stylesheet
 * and has nothing to do with case).
 */
export function countColorDeclarationsTextually(cssText) {
  const scrubbed = blankOut(cssText, { strings: true });
  const m = scrubbed.match(/(?:^|[;{}])\s*color\s*:/gim);
  return m ? m.length : 0;
}

/**
 * Line numbers of every line in `cssText` that LOOKS like a `color:`
 * declaration, found textually. Paired with `findColorDeclarations` this
 * answers "did the parser stop early?" — a postcss walk that bails out silently
 * still returns a plausible-looking list, and only a second opinion on WHICH
 * lines it should have visited exposes it.
 */
export function colorDeclarationLinesTextually(cssText) {
  const scrubbed = blankOut(cssText, { strings: true });
  const lines = [];
  scrubbed.split("\n").forEach((line, i) => {
    if (/(?:^|[;{}])\s*color\s*:/i.test(line)) lines.push(i + 1);
  });
  return lines;
}
