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
 * criterion defines as >= 18pt (24px) at any weight, or >= 14pt (18.667px) at
 * weight >= 700.
 *
 * Exported and pure so the boundary itself can be tested at the values that
 * matter, rather than only through whatever sizes this app happens to ship.
 */
export function wcagFloor(fontSizePx: number, fontWeight: number): number {
  const large = fontSizePx >= 24 || (fontSizePx >= 18.666 && fontWeight >= 700);
  return large ? 3 : 4.5;
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

export type FloorSweep = {
  /** Every element/pseudo pair the walk visited. */
  examined: number;
  /** How many of those actually painted visible text and were measured. */
  measured: number;
  /** Text runs below their floor. */
  findings: ContrastFinding[];
  /** Text runs whose backdrop this compositor refuses to model. */
  blocked: string[];
};

declare global {
  interface Window {
    __cv396: {
      /** [r, g, b, a] for any CSS colour the engine can parse; null when it cannot. */
      parse(css: string): number[] | null;
      /** The element's own background composited over its ancestors', over white. */
      backdrop(selector: string, index: number): number[] | null;
      /** Ancestors whose paint this compositor cannot model, for the precondition check. */
      opaqueBlockers(selector: string, index: number): string[];
      /** Every element (and ::before/::after/::placeholder) rendering `rgb`, by EITHER glyph-painting property. */
      sweep(rgb: number[]): { examined: number; offenders: string[] };
      /** Every visible text run scored against its WCAG floor. */
      floorSweep(): FloorSweep;
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
     * the walk stops at the first ancestor whose own `background-color` is
     * FULLY OPAQUE — nothing further up can paint through it, so a gradient on
     * `<body>` is irrelevant to a chip sitting on an opaque card. Without it
     * every element under any decorated ancestor is reported as unmeasurable
     * and the sweep is vacuous over exactly the page it is meant to check. The
     * stopping element's OWN background-image still counts: an image paints
     * over that element's background-color.
     */
    const blockersOf = (el: Element | null, stopAtOpaque: boolean): string[] => {
      if (!el) return ["<no such element>"];
      const bad: string[] = [];
      for (let n: Element | null = el; n; n = n.parentElement) {
        const s = getComputedStyle(n);
        const name = nameOf(n);
        if (s.backgroundImage !== "none") bad.push(`${name} background-image:${s.backgroundImage}`);
        if (s.filter !== "none") bad.push(`${name} filter:${s.filter}`);
        if (s.backdropFilter && s.backdropFilter !== "none")
          bad.push(`${name} backdrop-filter:${s.backdropFilter}`);
        if (Number(s.opacity) < 1) bad.push(`${name} opacity:${s.opacity}`);
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
          if (own && (own[3] ?? 1) > 0.999) break;
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

    /** WCAG 2.1 large text: >= 24px any weight, or >= 18.667px at weight >= 700. */
    const floorFor = (size: number, weight: number) =>
      size >= 24 || (size >= 18.666 && weight >= 700) ? 3 : 4.5;

    /**
     * Does this element/pseudo pair paint GLYPHS a reader can see?
     *
     * The element itself counts only when it owns a non-whitespace DIRECT child
     * text node — otherwise every wrapper `<div>` in the tree would be scored
     * for text its children paint, at whatever colour the wrapper inherited,
     * and the sweep would report the same run many times over under the wrong
     * colour.
     */
    const paintsText = (el: Element, pseudo: string | undefined): boolean => {
      if (pseudo === "::placeholder") {
        const input = el as HTMLInputElement | HTMLTextAreaElement;
        return !!input.placeholder && input.placeholder.trim().length > 0;
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

    /** Rendered at a non-zero size and not `visibility: hidden`. */
    const isVisible = (el: Element): boolean => {
      const s = getComputedStyle(el);
      if (s.visibility !== "visible") return false;
      const rects = el.getClientRects();
      for (const r of Array.from(rects)) if (r.width > 0.5 && r.height > 0.5) return true;
      return false;
    };

    window.__cv396 = {
      parse,
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
        const blocked: string[] = [];
        let examined = 0;
        let measured = 0;
        const all: Element[] = [document.body, ...Array.from(document.body.querySelectorAll("*"))];
        for (const el of all) {
          const pseudos: (string | undefined)[] = [undefined, "::before", "::after"];
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
            const what = describe(el, p ? p : "");
            if (s.blockers.length) {
              blocked.push(`${what} sits under ${s.blockers.join("; ")}`);
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
        return { examined, measured, findings, blocked };
      },
    };
  });
}
