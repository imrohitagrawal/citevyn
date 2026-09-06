/**
 * Shared helpers + design-token constants for the CiteVyn UI test suite.
 *
 * Token RGB values are copied verbatim from the design source-of-truth
 * (`CiteVyn Landing v2.dc.html` — LIGHT/DARK var blocks). Tests assert computed
 * styles against these so any drift from the design fails the build.
 */
import { Page, expect, type Locator } from "@playwright/test";
import path from "path";
import { createRequire } from "module";

// This spec runs as an ESM module, so `require` is not global — synthesize one.
const require = createRequire(import.meta.url);

/**
 * Decode a PNG buffer using the pngjs that Playwright already bundles, so the
 * legibility tests can sample real rendered pixels without adding a dependency.
 */
function loadPNG(): { sync: { read(buf: Buffer): { width: number; height: number; data: Buffer } } } {
  let bundle: { PNG?: { sync?: { read?: unknown } } };
  try {
    const base = path.dirname(require.resolve("playwright-core"));
    bundle = require(path.join(base, "lib", "utilsBundle.js"));
  } catch (e) {
    throw new Error(
      "Could not load Playwright's bundled pngjs from " +
        "playwright-core/lib/utilsBundle.js — the highlight-legibility tests decode " +
        "PNGs with it. Is playwright-core installed under Node 22? Original error: " +
        (e as Error).message,
    );
  }
  if (typeof bundle?.PNG?.sync?.read !== "function") {
    throw new Error(
      "Playwright's bundled utils no longer expose PNG.sync.read; the pngjs decode " +
        "path in tests/helpers.ts (loadPNG) needs updating to the new location/shape.",
    );
  }
  return bundle.PNG as { sync: { read(buf: Buffer): { width: number; height: number; data: Buffer } } };
}

/** Rec.601 luma of an 8-bit RGB triple (0..255). */
function luma(r: number, g: number, b: number) {
  return 0.299 * r + 0.587 * g + 0.114 * b;
}

/**
 * Screenshot a highlighter span and measure how much of a horizontal pixel row
 * through the CAP region of its FIRST text line is a "bright" backdrop — i.e. a
 * legible surface behind the dark --hl-ink letters (either the yellow band or,
 * in light mode, the light page canvas). The dark-mode page/card canvas is NOT
 * bright, so a heading whose yellow band only covers the bottom 40% leaves its
 * cap region sitting on the dark canvas and reads as dark-ink-on-dark.
 *
 * Returns the fraction of pixels in that row with luma ≥ `threshold`. Using the
 * first client rect keeps this correct even when an inline highlight wraps.
 */
export async function highlightBackdropBrightFraction(
  el: Locator,
  { fracY = 0.3, threshold = 100 }: { fracY?: number; threshold?: number } = {}
): Promise<number> {
  await el.scrollIntoViewIfNeeded();
  const geo = await el.evaluate((node) => {
    const rects = (node as Element).getClientRects();
    const box = (node as Element).getBoundingClientRect();
    return { firstLineH: rects[0]?.height ?? box.height, boxH: box.height };
  });
  const buf = await el.screenshot();
  const png = loadPNG().sync.read(buf);
  const { width, height, data } = png;
  const scale = height / geo.boxH; // device px per CSS px (DPR 1 here → ≈1)
  const y = Math.min(height - 1, Math.max(0, Math.round(fracY * geo.firstLineH * scale)));
  let bright = 0;
  for (let x = 0; x < width; x++) {
    const i = (y * width + x) * 4;
    if (luma(data[i], data[i + 1], data[i + 2]) >= threshold) bright++;
  }
  return width > 0 ? bright / width : 0;
}

/** Design tokens as computed-style rgb() strings, per theme. */
export const TOKENS = {
  light: {
    bg: "rgb(250, 249, 246)", // #faf9f6
    surface: "rgb(255, 255, 255)", // #ffffff
    surface2: "rgb(243, 241, 234)", // #f3f1ea
    ink: "rgb(28, 27, 25)", // #1c1b19
    muted: "rgb(107, 104, 98)", // #6b6862
    faint: "rgb(154, 151, 143)", // #9a978f
    border: "rgb(231, 227, 218)", // #e7e3da
    border2: "rgb(220, 215, 204)", // #dcd7cc
    hl: "rgb(255, 215, 94)", // #ffd75e
    hlSoft: "rgb(251, 233, 176)", // #fbe9b0
  },
  dark: {
    bg: "rgb(22, 22, 24)", // #161618
    surface: "rgb(30, 30, 33)", // #1e1e21
    surface2: "rgb(38, 38, 43)", // #26262b
    ink: "rgb(240, 239, 233)", // #f0efe9
    muted: "rgb(164, 161, 154)", // #a4a19a
    faint: "rgb(111, 109, 103)", // #6f6d67
    border: "rgb(50, 50, 56)", // #323238
    border2: "rgb(61, 61, 68)", // #3d3d44
    hl: "rgb(246, 196, 83)", // #f6c453
    hlSoft: "rgb(58, 51, 32)", // #3a3320
  },
} as const;

/** Theme-independent semantic colors (same in both modes). */
export const SEMANTIC = {
  success: "rgb(28, 154, 95)", // #1c9a5f
  amber: "rgb(180, 115, 42)", // #b4732a
  error: "rgb(194, 91, 78)", // #c25b4e
  errorChip: "rgb(176, 80, 63)", // #b0503f
};

export type ThemeName = "light" | "dark";

/**
 * Navigate to the app, tolerating Vite's cold-start dependency-optimize reload
 * (which aborts a "domcontentloaded" goto). "commit" + a generous mount wait
 * is the reliable pattern for a Vite dev server under Playwright.
 */
export async function gotoApp(page: Page) {
  await page.goto("/", { waitUntil: "commit" });
  await page.waitForSelector(".theme-toggle", { timeout: 30000 });
}

/**
 * Drive the real theme toggle until the app is in `theme`. The toggle label
 * shows the mode it switches TO, so "LIGHT" on the button means we're in dark.
 */
export async function ensureTheme(page: Page, theme: ThemeName) {
  const btn = page.locator(".theme-toggle");
  const label = (await btn.innerText()).trim();
  const currentlyDark = label.includes("LIGHT");
  if (theme === "dark" && !currentlyDark) await btn.click();
  if (theme === "light" && currentlyDark) await btn.click();
  await expect
    .poll(async () =>
      page.evaluate(() =>
        getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()
      )
    )
    .toBe(theme === "dark" ? "#161618" : "#faf9f6");
  // Root vars flip instantly, but element background/border colors have a
  // ~150ms transition. Wait it out so computed-style reads see the settled value.
  await page.waitForTimeout(400);
}

/** Enter the chat view via the header "Try the demo" button. */
export async function enterChat(page: Page) {
  await page.locator(".cta-button", { hasText: "Try the demo" }).click();
  await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
}

/**
 * Resolve a CSS color expression (e.g. `var(--hl-ink)`) to its computed rgb()
 * string by probing a throwaway element. Lets a test assert against the ACTUAL
 * token value rather than a hardcoded copy that could silently drift.
 */
export async function resolveColor(page: Page, value: string): Promise<string> {
  return page.evaluate((v) => {
    const probe = document.createElement("span");
    probe.style.color = v;
    document.body.appendChild(probe);
    const c = getComputedStyle(probe).color;
    probe.remove();
    return c;
  }, value);
}

/** Computed CSS property for the first match of a locator. */
export function computed(page: Page, selector: string, prop: string) {
  return page
    .locator(selector)
    .first()
    .evaluate((el, p) => getComputedStyle(el as Element)[p as any], prop);
}

/**
 * Wait until the answer is actually finished — no request in flight AND no
 * streaming caret left anywhere.
 *
 * The pending bubble has to be part of the condition. On the live path there is
 * a window between submitting a question and its first chunk in which NO caret
 * exists yet: the answer bubble has not been created, so the caret-only version
 * of this helper returned IMMEDIATELY and the caller went on to type its next
 * question while the previous one was still in flight. That was invisible until
 * something depended on it — nothing in the suite did, so a live test could
 * "wait for the answer" and wait for nothing at all. In demo mode there is
 * never a pending bubble, so the added condition is a no-op there.
 */
export async function waitStreamDone(page: Page, timeout = 12000) {
  await page.waitForFunction(
    () =>
      !document.querySelector(".pending-bubble") &&
      !document.querySelector(".typing-cursor"),
    { timeout },
  );
}

// ---------------------------------------------------------------------------
// WCAG contrast
// ---------------------------------------------------------------------------

/**
 * WCAG 2.x relative luminance of an 8-bit sRGB triple.
 *
 * Kept here rather than inlined in a spec so the focus-ring guard and any
 * future colour assertion share ONE implementation. The threshold constant
 * that matters for a focus indicator is 3:1 (WCAG 2.4.11 Focus Appearance),
 * not the 4.5:1 that applies to body text.
 */
export function relativeLuminance([r, g, b]: readonly number[]): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** WCAG 2.x contrast ratio between two opaque 8-bit sRGB triples (1..21). */
export function contrastRatio(a: readonly number[], b: readonly number[]): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

// ---------------------------------------------------------------------------
// Focus-ring measurement, from rendered pixels
// ---------------------------------------------------------------------------

/**
 * What does focusing this control actually CHANGE on screen, and is the change
 * visible?
 *
 * WHY PIXELS. Two rounds of this guard composited `getComputedStyle`
 * background colours up the ancestor chain and reported a confident 16.35:1 for
 * rings a skeptic then measured, from screenshots, at 1.00:1. First a gradient
 * ancestor (`background-color` is transparent, so the walk fell through to the
 * page canvas), then `::before`/`::after` overlays (invisible to
 * `getComputedStyle(n)` entirely). Each fix closed the demonstrated instance and
 * left the CLASS open, because it keyed on a proxy. Six shapes still defeated
 * it: an absolutely-positioned SIBLING over the control, a `transform` that
 * moves the control onto a non-ancestor panel, `filter: invert(0.5)` and
 * `backdrop-filter: contrast(0)` (both flatten ring, text and backdrop to a
 * uniform #808080), `opacity` on an ancestor, and the pseudo-elements.
 *
 * The composited OUTPUT answers all of them in one measurement, and it is also
 * the only thing a user can see. So: screenshot the control's neighbourhood
 * focused, blur it, screenshot again, and diff.
 *
 *   - changed pixels ARE the focus indicator, whatever draws it — an outline, a
 *     box-shadow, a border, a background swap. The guard stops caring which.
 *   - zero changed pixels means focusing this control changes nothing a sighted
 *     keyboard user could perceive, which is WCAG 2.4.7 failing, full stop.
 *   - contrast is then measured between the indicator's own pixels and the
 *     pixels immediately outside it, both read from the FOCUSED frame, which is
 *     what WCAG 2.4.11 means by "against adjacent colours".
 *
 * Returns null when the control has no box to sample (detached or zero-sized).
 */
export const MIN_PERCEIVABLE_RATIO = 3;

export async function measureFocusIndicator(
  page: Page,
  {
    pad = 8,
    minChangedPixels = 12,
    minStrongPixels = 8,
  }: { pad?: number; minChangedPixels?: number; minStrongPixels?: number } = {},
): Promise<{
  changedPixels: number;
  strongPixels: number;
  ratio: number | null;
  indicator: number[] | null;
  adjacent: number[] | null;
} | null> {
  // The element currently focused by the caller's real Tab press. Stashed
  // page-side so it can be blurred and re-focused without disturbing the walk;
  // keyboard modality is already established, so `:focus-visible` survives the
  // programmatic re-focus.
  const box = await page.evaluate(() => {
    const el = document.activeElement as HTMLElement | null;
    if (!el || el === document.body) return null;
    (window as unknown as { __frEl?: HTMLElement }).__frEl = el;
    let r = el.getBoundingClientRect();
    // A Tab press scrolls the control into view, but a control appended below
    // the fold (or one the browser only partially revealed) can still sit
    // outside the viewport — and a screenshot clip outside the viewport clamps
    // to zero and yields NO measurement. Bring it fully into view first; the
    // walk is already scrolling, so this changes nothing it was not doing.
    if (r.top < 0 || r.bottom > window.innerHeight) {
      el.scrollIntoView({ block: "center", behavior: "instant" as ScrollBehavior });
      r = el.getBoundingClientRect();
    }
    if (r.width === 0 || r.height === 0) return null;
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  });
  if (!box) return null;

  const vp = page.viewportSize();
  // Clamp to the viewport on BOTH edges. Clamping only the far edge produced a
  // negative width/height for anything off-screen, which read as "no
  // measurement" — a silent skip, which is the failure mode this whole file
  // exists to avoid.
  const left = Math.max(0, Math.floor(box.x - pad));
  const top = Math.max(0, Math.floor(box.y - pad));
  const right = Math.min(vp ? vp.width : box.x + box.width + pad, Math.ceil(box.x + box.width + pad));
  const bottom = Math.min(
    vp ? vp.height : box.y + box.height + pad,
    Math.ceil(box.y + box.height + pad),
  );
  const clip = { x: left, y: top, width: right - left, height: bottom - top };
  if (clip.width <= 2 || clip.height <= 2) return null;

  // NOTE: deliberately NOT `animations: "disabled"`. That option REWINDS
  // infinite animations to their start, so the marquee jumps between the
  // `getBoundingClientRect()` above and the screenshot below and the clip no
  // longer contains the control it measured — which showed up as a phantom
  // 2.48:1 on one ticker chip. `freezeAnimations()` pauses them WHERE THEY ARE
  // instead, so the rect and both frames agree. Call it before the walk.
  const shot = { clip };
  const focused = loadPNG().sync.read(await page.screenshot(shot));
  await page.evaluate(() => (window as unknown as { __frEl: HTMLElement }).__frEl.blur());
  const blurred = loadPNG().sync.read(await page.screenshot(shot));
  await page.evaluate(() =>
    (window as unknown as { __frEl: HTMLElement }).__frEl.focus({ preventScroll: true }),
  );

  const { width, height } = focused;
  if (blurred.width !== width || blurred.height !== height) return null;

  // Mark every pixel the focus state changed. The threshold is deliberately
  // low: a ring that only just differs is still a ring, and the CONTRAST
  // assertion below is what decides whether it is good enough.
  const changed = new Uint8Array(width * height);
  let changedPixels = 0;
  for (let i = 0; i < width * height; i++) {
    const o = i * 4;
    const d =
      Math.abs(focused.data[o] - blurred.data[o]) +
      Math.abs(focused.data[o + 1] - blurred.data[o + 1]) +
      Math.abs(focused.data[o + 2] - blurred.data[o + 2]);
    if (d > 12) {
      changed[i] = 1;
      changedPixels += 1;
    }
  }
  if (changedPixels < minChangedPixels) {
    return { changedPixels, strongPixels: 0, ratio: null, indicator: null, adjacent: null };
  }

  // The indicator's own colour, and the colour of the pixels touching it that
  // it did NOT change — its adjacent colour, read from the focused frame.
  const mean = (pick: (i: number) => boolean) => {
    let r = 0;
    let g = 0;
    let b = 0;
    let n = 0;
    for (let i = 0; i < width * height; i++) {
      if (!pick(i)) continue;
      const o = i * 4;
      r += focused.data[o];
      g += focused.data[o + 1];
      b += focused.data[o + 2];
      n += 1;
    }
    return n === 0 ? null : [Math.round(r / n), Math.round(g / n), Math.round(b / n)];
  };
  const touchesChanged = (i: number) => {
    if (changed[i]) return false;
    const x = i % width;
    const y = (i / width) | 0;
    for (let dy = -2; dy <= 2; dy++) {
      for (let dx = -2; dx <= 2; dx++) {
        const nx = x + dx;
        const ny = y + dy;
        if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
        if (changed[ny * width + nx]) return true;
      }
    }
    return false;
  };
  const adjacent = mean(touchesChanged);
  if (!adjacent) return { changedPixels, ratio: null, indicator: null, adjacent: null };

  // Judge the indicator by its STRONGEST pixels, not by the mean of everything
  // that moved.
  //
  // The mean is the wrong statistic here, and measuring taught that rather than
  // reasoning: raising the marquee track on `:focus-within` un-fades the whole
  // strip, so focusing one chip legitimately changes its NEIGHBOURS' pixels
  // too. Averaging a genuinely black 2px ring together with a neighbouring
  // chip's yellow tag produced rgb(187,164,94) and a verdict of 2.35:1 for a
  // ring a person can plainly see.
  //
  // WCAG 2.4.11 asks whether a perceivable indicator EXISTS against adjacent
  // colour, so count the pixels that clear the bar rather than averaging them
  // away. `minStrongPixels` stops a couple of stray antialiased pixels
  // qualifying: a real 2px ring around even a small control contributes
  // hundreds.
  let best = 1;
  let bestPx: number[] | null = null;
  let strong = 0;
  for (let i = 0; i < width * height; i++) {
    if (!changed[i]) continue;
    const o = i * 4;
    const px = [focused.data[o], focused.data[o + 1], focused.data[o + 2]];
    const r = contrastRatio(px, adjacent);
    if (r >= MIN_PERCEIVABLE_RATIO) strong += 1;
    if (r > best) {
      best = r;
      bestPx = px;
    }
  }
  return {
    changedPixels,
    strongPixels: strong,
    // `ratio` is the best contrast any part of the indicator achieves; the
    // caller pairs it with `strongPixels` so a lone antialiased pixel cannot
    // carry a verdict.
    ratio: best,
    indicator: bestPx,
    adjacent,
  };
}

/**
 * Hold the page still enough that a before/after screenshot pair differs only
 * by the focus state.
 *
 * Two things this deliberately does NOT do, each learned by measuring:
 *   - NOT `page.screenshot({ animations: "disabled" })`. That REWINDS infinite
 *     animations to their first frame, so a marquee item moves between the
 *     moment its rect is read and the moment it is captured, and the clip no
 *     longer contains the control — a phantom 2.31:1 on one ticker chip.
 *   - NOT a global `animation-play-state: paused`. The reveal-on-scroll
 *     animation starts at `opacity: 0`, so pausing it globally froze a whole
 *     section invisible and every control in it measured "focusing changes 0
 *     pixels" — a guard reporting a defect it had created itself.
 */
export async function freezeAnimations(page: Page) {
  await page.addStyleTag({
    content: `
      /* The marquee is the only animation that MOVES a focusable control, so it
       * is the only one that must hold still between the two frames. Pause it
       * where it is. */
      .ticker-track, .marquee-track {
        animation-play-state: paused !important;
      }
      /* Transitions off everywhere: a focus border mid-transition makes the two
       * frames differ by an amount that depends on timing rather than on
       * design. Setting it to none jumps straight to the settled value. */
      *, *::before, *::after { transition: none !important; }`,
  });
}
