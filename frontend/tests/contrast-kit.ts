/**
 * The page-side colour measurement kit, shared by the two rendered-contrast
 * suites.
 *
 * It was written for #396 (`faint-contrast.spec.ts`) and lived inside that file
 * as a private `installKit`. #403 needs the SAME compositor for a different
 * question — not "does anything render `--faint`?" but "does every text run
 * clear its WCAG floor?" — so it moved here rather than being copied. One
 * compositor, one place where a mis-measurement can be fixed.
 *
 * WHAT IT KNOWS HOW TO DO
 *   - parse ANY computed colour the engine emits, `color-mix()` and
 *     `color(srgb ...)` included, by painting it and reading the pixel back,
 *     recovering alpha rather than assuming 1;
 *   - composite an element's real painted backdrop up the ancestor chain;
 *   - report every ancestor whose paint that compositor cannot model, so a
 *     wrong answer is refused instead of returned confidently;
 *   - sweep the page for one exact rgb (#396's token ban);
 *   - sweep the page for every text run that misses its WCAG floor (#403).
 *
 * WHAT IT CANNOT SEE is documented per-suite, because the blind spots are
 * different: `faint-contrast.spec.ts` lists the token-ban ones and
 * `contrast-floor.spec.ts` lists the floor ones.
 */
import { contrastRatio } from "./helpers";

/**
 * The contrast ratio of text that may be TRANSLUCENT, composited onto the
 * backdrop it paints over first.
 *
 * `contrastRatio(colour.slice(0, 3), bg)` scores the un-premultiplied colour as
 * if it were opaque. `.cta-footnote` in this app's stylesheet is
 * `color-mix(in srgb, var(--bg) 65%, transparent)`, so the shape is live, not
 * hypothetical: at 50% alpha the opaque reading is roughly double the ratio a
 * reader actually gets.
 */
export function paintedRatio(colour: readonly number[], backdrop: readonly number[]): number {
  const a = colour[3] ?? 1;
  const over = [0, 1, 2].map((i) => Math.round(a * colour[i] + (1 - a) * backdrop[i]));
  return contrastRatio(over, backdrop);
}

/**
 * WCAG 2.1 SC 1.4.3 AA. 4.5:1 for normal text; 3:1 for LARGE text, which the
 * criterion defines as >= 18pt at any weight, or >= 14pt at weight >= 700.
 *
 * IN POINTS, not in a px constant. An earlier version wrote the 14pt bound as
 * `>= 18.666`, and 14pt is 18.6666…px — so an 18.666px/700 run was given the
 * relaxed 3:1 floor while measuring 13.9995pt, i.e. normal text. A reviewer
 * planted exactly that and the sweep granted it 3:1. `px * 0.75` removes the
 * constant instead of retuning it.
 *
 * THIS FUNCTION IS NOT WHAT THE SWEEP RUNS. `installKit` serializes its own
 * copy into the page (the init script cannot close over module scope), so
 * pinning the boundary here proves nothing about the floor that decides real
 * findings. `contrast-floor.spec.ts` therefore asserts the boundary through
 * `window.__cv396.measure`'s REPORTED floor, on planted elements, and this
 * export is kept only for callers outside the page.
 *
 * THE FIRST FIX ONLY REACHED THIS COPY. `px * 0.75` landed here while the
 * in-page `floorFor` kept the truncated `18.666`, so the defect described above
 * stayed live in the copy that decides findings, and the two rules disagreed
 * across [18.666, 18.6666…)px at weight >= 700. Both now carry the identical
 * expression; `contrast-floor.spec.ts` asserts they agree across exactly that
 * window by calling `window.__cv396.floorFor` directly (no element, so
 * Chromium's 1/64px font-size quantisation cannot hide it); and no spec writes
 * a third copy — the header test calls the kit's.
 */
export function wcagFloor(fontSizePx: number, fontWeight: number): number {
  const pt = fontSizePx * 0.75;
  return pt >= 18 || (pt >= 14 && fontWeight >= 700) ? 3 : 4.5;
}

/** One text run that misses its floor. */
export type ContrastFinding = {
  /** `tag.class::pseudo — "the text"`, enough to find it in the DOM. */
  what: string;
  /** The computed ratio of the painted glyph colour over its painted backdrop. */
  ratio: number;
  /** The floor that ratio had to clear, from `wcagFloor`. */
  floor: number;
  fontSize: number;
  fontWeight: number;
  /** `rgb(r, g, b)` of the composited backdrop. */
  backdrop: string;
  /** `rgba(r, g, b, a)` of the painted glyph colour. */
  colour: string;
};

/** One named text run measured on demand, whether or not it clears its floor. */
export type MeasuredRun = {
  ratio: number;
  floor: number;
  fontSize: number;
  fontWeight: number;
  backdrop: string;
  colour: string;
  /** Non-empty when the compositor refuses to answer for this run. */
  blockers: string[];
};

/** A text run the compositor refuses to score, with every reason separately. */
export type BlockedRun = {
  /** `tag.class::pseudo — "the text"`. */
  what: string;
  /** Each unmodellable ancestor, ONE PER ENTRY — never pre-joined.
   *
   * Joining them was a demonstrated bypass: `ALLOWED_BLOCKERS` matched with
   * `entry.includes(prefix)`, so a run under BOTH an allowed shape and a new
   * unexplained one was waved through on the allowed half. Reproduced by a
   * reviewer with a `filter: contrast(0.2)` element inside the header. */
  blockers: string[];
};

export type FloorSweep = {
  /** Every element/pseudo pair the walk visited, painting text or not. */
  examined: number;
  /**
   * Every element/pseudo pair that is VISIBLE and paints text — the population
   * this sweep is responsible for.
   *
   * The derived non-vacuity partner: `measured + blocked.length` must equal
   * this, in every sweep. A threshold on `examined` cannot do that job —
   * `examined` counts DOM nodes, not measurements, and a reviewer hid ten of
   * the landing page's eleven sections one at a time while `examined` stayed at
   * 1279 throughout and `measured` fell from 260 to 10.
   */
  textRuns: number;
  /** How many were actually scored. */
  measured: number;
  /** Text runs below their floor. */
  findings: ContrastFinding[];
  /** Text runs whose backdrop this compositor refuses to model. */
  blocked: BlockedRun[];
};

declare global {
  interface Window {
    __cv396: {
      /** [r, g, b, a] for any CSS colour the engine can parse; null when it cannot. */
      parse(css: string): number[] | null;
      /**
       * The WCAG floor this run owes, as a PURE function — the same rule the
       * exported `wcagFloor` states, and the single one any in-page caller may
       * use. Exposed so no spec writes a third copy of it.
       */
      floorFor(fontSizePx: number, fontWeight: number): number;
      /** The element's own background composited over its ancestors', over white. */
      backdrop(selector: string, index: number): number[] | null;
      /** Ancestors whose paint this compositor cannot model, for the precondition check. */
      opaqueBlockers(selector: string, index: number): string[];
      /** Every element (and ::before/::after/::placeholder) rendering `rgb`, by EITHER glyph-painting property. */
      sweep(rgb: number[]): { examined: number; offenders: string[] };
      /** Every visible text run scored against its WCAG floor. */
      floorSweep(): FloorSweep;
      /** The rendered rgb of one CSS colour expression, resolved on a probe element. */
      resolve(css: string): number[] | null;
      /** One named text run, scored the same way, for a probe with a stated expectation. */
      measure(selector: string, index: number, pseudo?: string): MeasuredRun | null;
      /** The computed `color` of one match, as [r, g, b, a]. */
      colorOf(selector: string, index: number, pseudo?: string): number[] | null;
      /** `color` AND `-webkit-text-fill-color` of one match, both as [r, g, b, a]. */
      paintOf(
        selector: string,
        index: number,
      ): { color: number[] | null; fill: number[] | null } | null;
      count(selector: string): number;
    };
  }
}

/**
 * Install the page-side measurement kit. Registered BEFORE navigation so it
 * survives the app's own mount, and the SPA never reloads, so it is still there
 * after `enterChat`.
 */
export async function installKit(page: import("@playwright/test").Page) {
  await page.addInitScript(() => {
    const canvas = document.createElement("canvas");
    canvas.width = 1;
    canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });

    const memo = new Map<string, number[] | null>();

    /**
     * Parse ANY computed colour — including `color-mix()` and `color(srgb ...)`,
     * which `getComputedStyle` emits and a hand-written `rgb()` regex does not
     * understand — by painting it and reading the pixel back.
     *
     * TWO SENTINELS first: assigning an unparseable value to `fillStyle` is a
     * no-op, so the property keeps whatever it held. Assigning the candidate
     * after two DIFFERENT sentinels and comparing the reads is what tells
     * "parsed to red" apart from "not parsed, and red was left over".
     *
     * Alpha is then recovered rather than assumed: painting the colour over
     * white and over black gives `over_white - over_black = (1 - a) * 255` per
     * channel, so `a` falls out and the un-premultiplied colour with it.
     */
    const parse = (css: string): number[] | null => {
      if (memo.has(css)) return memo.get(css) ?? null;
      let result: number[] | null = null;
      if (ctx) {
        ctx.fillStyle = "#ff0000";
        ctx.fillStyle = css;
        const first = ctx.fillStyle;
        ctx.fillStyle = "#00ff00";
        ctx.fillStyle = css;
        const second = ctx.fillStyle;
        if (first === second) {
          const over = (base: string) => {
            ctx.globalCompositeOperation = "source-over";
            ctx.clearRect(0, 0, 1, 1);
            ctx.fillStyle = base;
            ctx.fillRect(0, 0, 1, 1);
            ctx.fillStyle = css;
            ctx.fillRect(0, 0, 1, 1);
            return ctx.getImageData(0, 0, 1, 1).data;
          };
          const w = over("#ffffff");
          const k = over("#000000");
          const a = 1 - (w[0] - k[0] + (w[1] - k[1]) + (w[2] - k[2])) / 3 / 255;
          result = a <= 0.0001 ? [0, 0, 0, 0] : [k[0] / a, k[1] / a, k[2] / a, a];
        }
      }
      memo.set(css, result);
      return result;
    };

    const nth = (selector: string, index: number): Element | null =>
      document.querySelectorAll(selector)[index] ?? null;

    /** Layer the element's own background, then each ancestor's, over the page canvas. */
    const backdropOf = (el: Element | null): number[] | null => {
      if (!el) return null;
      const layers: number[][] = [];
      for (let n: Element | null = el; n; n = n.parentElement) {
        const c = parse(getComputedStyle(n).backgroundColor);
        if (c && c[3] > 0.001) layers.push(c);
      }
      // The browser paints white under <html> when nothing else does.
      let out = [255, 255, 255];
      for (let i = layers.length - 1; i >= 0; i--) {
        const [lr, lg, lb, la] = layers[i];
        out = [
          la * lr + (1 - la) * out[0],
          la * lg + (1 - la) * out[1],
          la * lb + (1 - la) * out[2],
        ];
      }
      return [Math.round(out[0]), Math.round(out[1]), Math.round(out[2])];
    };

    const backdrop = (selector: string, index: number): number[] | null =>
      backdropOf(nth(selector, index));

    const nameOf = (n: Element) =>
      `${n.tagName.toLowerCase()}${
        n.className && typeof n.className === "string"
          ? "." + n.className.trim().split(/\s+/).join(".")
          : ""
      }`;

    /**
     * Everything in the ancestor chain that would make the composite above a
     * LIE: a gradient or image background, an ancestor `opacity`, a `filter` or
     * `backdrop-filter`, or a pseudo-element with real `content` that paints
     * something of its own.
     *
     * The pseudo pass is not decoration on this list — it is the shape this app
     * ACTUALLY has. `.ticker-strip::before/::after` are 80px gradients at
     * `z-index: 1`, and `landing.css` records them painting over a focus ring
     * down to 1.47:1.
     *
     * "Paints something" is `content` other than `none` AND either a
     * non-transparent `background-color` or a `background-image`. A pseudo that
     * only contributes TEXT does not obscure the backdrop, and reporting it
     * would block every measured element in the app for nothing.
     *
     * `stopAtOpaque` is what makes the whole-page floor sweep usable. With it,
     * the BACKGROUND half of the walk stops at the first ancestor whose own
     * `background-color` is fully opaque — nothing behind that can paint
     * through, so a gradient on `<body>` is irrelevant to a chip sitting on an
     * opaque card. Without it every element under any decorated ancestor is
     * reported as unmeasurable and the sweep is vacuous over exactly the page
     * it is meant to check. The stopping element's OWN background-image still
     * counts: an image paints over that element's background-color.
     *
     * WHAT KEEPS WALKING PAST THE OPAQUE STOP, and why an earlier version was
     * unsound. `opacity`, `filter`, `backdrop-filter` and `mix-blend-mode` on
     * an ancestor do not paint BEHIND the text — they transform the whole
     * subtree, text and background together, after it is drawn. An opaque
     * background underneath does not protect against any of them. A reviewer
     * measured all four against real screenshot pixels: a group `opacity: .5`
     * over an opaque white card read 4.54:1 from this compositor and 1.96:1
     * from the pixels; `filter: contrast(.18)` read 4.54 against a real 4.06;
     * `mix-blend-mode: screen` read 21:1 against a real 1.00:1, with the glyphs
     * invisible. So those four are collected for the whole chain and only the
     * background shapes stop early.
     *
     * STILL NOT MODELLED, because no ancestor walk can see it: an
     * absolutely-positioned SIBLING painting over the text. The same reviewer
     * measured `rgba(255,255,255,.93)` over a legible run as 21:1 reported
     * against 1.17:1 real. `frontend/tests/focus-ring.spec.ts` answers that
     * class from rendered PIXELS; this compositor cannot, and says so rather
     * than implying otherwise.
     */
    const blockersOf = (el: Element | null, stopAtOpaque: boolean): string[] => {
      if (!el) return ["<no such element>"];
      const bad: string[] = [];
      let behindIsHidden = false;
      for (let n: Element | null = el; n; n = n.parentElement) {
        const s = getComputedStyle(n);
        const name = nameOf(n);
        // These four survive an opaque background underneath them.
        if (s.filter !== "none") bad.push(`${name} filter:${s.filter}`);
        if (s.backdropFilter && s.backdropFilter !== "none")
          bad.push(`${name} backdrop-filter:${s.backdropFilter}`);
        if (Number(s.opacity) < 1) bad.push(`${name} opacity:${s.opacity}`);
        if (s.mixBlendMode && s.mixBlendMode !== "normal")
          bad.push(`${name} mix-blend-mode:${s.mixBlendMode}`);
        if (!behindIsHidden) {
          if (s.backgroundImage !== "none")
            bad.push(`${name} background-image:${s.backgroundImage}`);
          for (const pseudo of ["::before", "::after"]) {
            const ps = getComputedStyle(n, pseudo);
            if (!ps.content || ps.content === "none") continue;
            const pbg = parse(ps.backgroundColor);
            const paints = (pbg && pbg[3] > 0.001) || ps.backgroundImage !== "none";
            if (paints) {
              bad.push(
                `${name}${pseudo} content:${ps.content} background:${
                  ps.backgroundImage !== "none" ? ps.backgroundImage : ps.backgroundColor
                }`,
              );
            }
          }
          if (stopAtOpaque) {
            const own = parse(s.backgroundColor);
            if (own && (own[3] ?? 1) > 0.999) behindIsHidden = true;
          }
        }
      }
      return bad;
    };

    const opaqueBlockers = (selector: string, index: number): string[] =>
      blockersOf(nth(selector, index), false);

    const describe = (el: Element, pseudo: string) => {
      const cls =
        typeof el.className === "string" && el.className
          ? `.${el.className.trim().split(/\s+/).join(".")}`
          : "";
      const text = (el.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 60);
      return `${el.tagName.toLowerCase()}${cls}${pseudo}${text ? ` — "${text}"` : ""}`;
    };

    const same = (a: number[] | null, rgb: number[]) =>
      !!a &&
      a[3] > 0.05 &&
      Math.round(a[0]) === rgb[0] &&
      Math.round(a[1]) === rgb[1] &&
      Math.round(a[2]) === rgb[2];

    /**
     * WCAG 2.1 large text: >= 18pt any weight, or >= 14pt at weight >= 700.
     *
     * ONE RULE, WRITTEN TWICE, KEPT IDENTICAL BY TEST. `addInitScript`
     * serializes this function into the page and an init script cannot close
     * over module scope, so this copy cannot literally BE the exported
     * `wcagFloor` above — but it is now the same expression, in points rather
     * than the truncated `18.666` px it used to carry. That truncation made the
     * two copies disagree across [18.666, 18.6666…)px at weight >= 700: the
     * in-page copy said 3, the exported one said 4.5. No element could expose
     * it (Chromium quantises font-size to 1/64px), which is why the boundary
     * test in `contrast-floor.spec.ts` missed it for as long as it existed.
     * That test now also calls this function DIRECTLY, through
     * `window.__cv396.floorFor`, across exactly that window.
     */
    const floorFor = (size: number, weight: number) => {
      const pt = size * 0.75;
      return pt >= 18 || (pt >= 14 && weight >= 700) ? 3 : 4.5;
    };

    /**
     * Does this element/pseudo pair paint GLYPHS a reader can see?
     *
     * The element itself counts only when it owns a non-whitespace DIRECT child
     * text node — otherwise every wrapper `<div>` in the tree would be scored
     * for text its children paint, at whatever colour the wrapper inherited,
     * and the sweep would report the same run many times over under the wrong
     * colour.
     */
    /** Pseudos that restyle the element's OWN text rather than adding any. */
    const TEXT_STYLING_PSEUDOS = ["::first-line", "::first-letter", "::marker"];

    const paintsText = (el: Element, pseudo: string | undefined): boolean => {
      if (pseudo === "::placeholder") {
        const input = el as HTMLInputElement | HTMLTextAreaElement;
        return !!input.placeholder && input.placeholder.trim().length > 0;
      }
      if (pseudo && TEXT_STYLING_PSEUDOS.indexOf(pseudo) >= 0) {
        // These paint the element's own glyphs at their own colour, so they
        // exist exactly when the element does. `::marker` additionally needs a
        // list item. A reviewer planted `p::first-line`, `p::first-letter` and
        // `li::marker` at 2.85:1 and the sweep reported none of them, because
        // the pseudo list stopped at ::before/::after/::placeholder.
        if (pseudo === "::marker" && getComputedStyle(el).display !== "list-item") return false;
        return paintsText(el, undefined);
      }
      if (pseudo) {
        const content = getComputedStyle(el, pseudo).content;
        if (!content || content === "none" || content === "normal") return false;
        // `content` arrives quoted. A counter, image or attr() value is not a
        // literal string and is not scored as text.
        const literal = /^"(.*)"$/s.exec(content);
        return !!literal && literal[1].trim().length > 0;
      }
      for (const node of Array.from(el.childNodes)) {
        if (node.nodeType === Node.TEXT_NODE && (node.textContent ?? "").trim().length > 0) {
          return true;
        }
      }
      return false;
    };

    /** WCAG relative luminance of an 8-bit sRGB triple. */
    const lum = (rgb: number[]) => {
      const c = rgb.map((v) => {
        const x = v / 255;
        return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4);
      });
      return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
    };

    /**
     * Score ONE element/pseudo pair: the effective painted glyph colour,
     * composited onto its real backdrop, against the floor its size and weight
     * earn. `null` when the pair paints no visible glyphs.
     */
    const score = (el: Element, pseudo: string | undefined) => {
      const s = getComputedStyle(el, pseudo ?? undefined);
      // The EFFECTIVE painted colour. When `-webkit-text-fill-color` is unset
      // Chromium resolves it TO `color`; when it is set it WINS, and reading
      // `color` would score a glyph that is not on screen.
      const colour = parse(s.webkitTextFillColor) ?? parse(s.color);
      if (!colour) return null;
      const bg = backdropOf(el);
      if (!bg) return null;
      const a = colour[3] ?? 1;
      const over = [0, 1, 2].map((i) => Math.round(a * colour[i] + (1 - a) * bg[i]));
      const l1 = lum(over);
      const l2 = lum(bg);
      const fontSize = parseFloat(s.fontSize);
      const fontWeight = Number(s.fontWeight) || 400;
      return {
        alpha: a,
        ratio: (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05),
        floor: floorFor(fontSize, fontWeight),
        fontSize,
        fontWeight,
        backdrop: `rgb(${bg.join(", ")})`,
        colour: `rgba(${colour.slice(0, 3).map(Math.round).join(", ")}, ${a.toFixed(3)})`,
        blockers: blockersOf(el, true),
      };
    };

    /**
     * Is this element's text something a SIGHTED reader actually sees, and is
     * SC 1.4.3 responsible for it?
     *
     * Two exclusions beyond the obvious, both added because a reviewer planted
     * them and the sweep reported a failure no reader could experience:
     *
     *   - THE VISUALLY-HIDDEN IDIOM. `landing.css`'s `.sr-only` is
     *     `width: 1px; height: 1px; clip-path: inset(50%)`, so it HAS a rect —
     *     a 1x1 one — and the old `> 0.5px` rule scored it. A planted
     *     `.sr-only` span at 2.88:1 was reported as a finding, over text that
     *     is on screen for nobody. `ChatView.tsx` ships two such regions. The
     *     bar is 2px in BOTH dimensions, which no real text run is under.
     *   - INACTIVE CONTROLS. SC 1.4.3 exempts "text or images of text that are
     *     part of an inactive user interface component". `.cta:disabled` is one
     *     ("a true no-op: a disabled button is inert to both mouse and
     *     keyboard, and removed from the tab order" — landing.css). It used to
     *     escape only incidentally, because it ALSO sets `opacity: 0.5` and so
     *     became a blocker; that is keying on the wrong thing, and it would
     *     stop working the day the opacity moved.
     */
    const isVisible = (el: Element): boolean => {
      const s = getComputedStyle(el);
      if (s.visibility !== "visible") return false;
      if (el.closest("[disabled], fieldset[disabled]")) return false;
      const rects = el.getClientRects();
      for (const r of Array.from(rects)) if (r.width > 2 && r.height > 2) return true;
      return false;
    };

    window.__cv396 = {
      parse,
      floorFor,
      backdrop,
      opaqueBlockers,
      count: (selector: string) => document.querySelectorAll(selector).length,
      colorOf: (selector: string, index: number, pseudo?: string) => {
        const el = nth(selector, index);
        if (!el) return null;
        return parse(getComputedStyle(el, pseudo ?? undefined).color);
      },
      paintOf: (selector: string, index: number) => {
        const el = nth(selector, index);
        if (!el) return null;
        const s = getComputedStyle(el);
        return { color: parse(s.color), fill: parse(s.webkitTextFillColor) };
      },
      sweep: (rgb: number[]) => {
        const offenders: string[] = [];
        let examined = 0;
        // `document.body`, not `document` — <head>, <script> and <meta> have a
        // computed colour that no reader can see, and counting them would
        // inflate the non-vacuity number this test reports.
        const all: Element[] = [document.body, ...Array.from(document.body.querySelectorAll("*"))];
        for (const el of all) {
          const pseudos: (string | undefined)[] = [undefined, "::before", "::after"];
          if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
            pseudos.push("::placeholder");
          }
          for (const p of pseudos) {
            examined += 1;
            const s = getComputedStyle(el, p ?? undefined);
            // BOTH glyph-painting properties. `-webkit-text-fill-color`
            // OVERRIDES `color` for the fill while `color` keeps reporting its
            // old value, so a `color`-only sweep reads 5.27:1 off an element
            // whose glyphs are painting at 2.77:1 — measured on
            // `.composer-hint`, and green on every gate at the time.
            // When it is unset Chromium resolves it TO `color`, so checking it
            // adds no offender that `color` would not already have named.
            if (same(parse(s.color), rgb)) {
              offenders.push(describe(el, p ? p : ""));
            } else if (same(parse(s.webkitTextFillColor), rgb)) {
              offenders.push(`${describe(el, p ? p : "")} [-webkit-text-fill-color]`);
            }
          }
        }
        return { examined, offenders };
      },
      resolve: (css: string) => {
        const probe = document.createElement("span");
        probe.style.color = css;
        document.body.appendChild(probe);
        const out = parse(getComputedStyle(probe).color);
        probe.remove();
        return out;
      },
      measure: (selector: string, index: number, pseudo?: string) => {
        const el = nth(selector, index);
        if (!el) return null;
        const s = score(el, pseudo);
        if (!s) return null;
        return {
          ratio: s.ratio,
          floor: s.floor,
          fontSize: s.fontSize,
          fontWeight: s.fontWeight,
          backdrop: s.backdrop,
          colour: s.colour,
          blockers: s.blockers,
        };
      },
      floorSweep: () => {
        const findings: {
          what: string;
          ratio: number;
          floor: number;
          fontSize: number;
          fontWeight: number;
          backdrop: string;
          colour: string;
        }[] = [];
        const blocked: { what: string; blockers: string[] }[] = [];
        let examined = 0;
        let measured = 0;
        let textRuns = 0;
        const all: Element[] = [document.body, ...Array.from(document.body.querySelectorAll("*"))];
        for (const el of all) {
          const pseudos: (string | undefined)[] = [
            undefined,
            "::before",
            "::after",
            ...TEXT_STYLING_PSEUDOS,
          ];
          if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) {
            pseudos.push("::placeholder");
          }
          const visible = isVisible(el);
          for (const p of pseudos) {
            examined += 1;
            if (!visible) continue;
            if (!paintsText(el, p)) continue;
            const s = score(el, p);
            if (!s) continue;
            // Fully transparent glyphs paint nothing. Alpha-0 labels are not a
            // contrast failure.
            if (s.alpha <= 0.05) continue;
            // Counted BEFORE the blocker branch: this is the population the
            // sweep is responsible for, and `measured + blocked` must account
            // for all of it.
            textRuns += 1;
            const what = describe(el, p ? p : "");
            if (s.blockers.length) {
              blocked.push({ what, blockers: s.blockers });
              continue;
            }
            measured += 1;
            if (s.ratio < s.floor) {
              findings.push({
                what,
                ratio: s.ratio,
                floor: s.floor,
                fontSize: s.fontSize,
                fontWeight: s.fontWeight,
                backdrop: s.backdrop,
                colour: s.colour,
              });
            }
          }
        }
        return { examined, textRuns, measured, findings, blocked };
      },
    };
  });
}
