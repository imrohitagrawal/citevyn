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

/**
 * Semantic colors, PER THEME as of #403.
 *
 * This was one flat map headed "theme-independent semantic colors (same in both
 * modes)", and that line described the defect rather than the design: one value
 * per role cannot clear WCAG AA on both #ffffff and #1e1e21, and seven text
 * runs shipped below 4.5:1 because of it. The shape now mirrors `TOKENS` above
 * — index it by theme — so a caller cannot quietly assert a light colour inside
 * a dark test.
 *
 * `amber` is `--color-warning` (which `--refusal-amber` aliases); `errorChip`
 * is `--color-error-strong`.
 */
export const SEMANTIC = {
  light: {
    success: "rgb(21, 122, 74)", // #157a4a
    amber: "rgb(138, 84, 24)", // #8a5418
    error: "rgb(168, 68, 55)", // #a84437
    errorChip: "rgb(138, 51, 36)", // #8a3324
  },
  dark: {
    success: "rgb(47, 174, 114)", // #2fae72
    amber: "rgb(224, 164, 88)", // #e0a458
    error: "rgb(224, 139, 125)", // #e08b7d
    errorChip: "rgb(245, 171, 154)", // #f5ab9a
  },
} as const;

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

// ---------------------------------------------------------------------------
// In-page timing recorders (#354)
// ---------------------------------------------------------------------------

/** One uninterrupted period during which a selector matched something. */
export type PresenceWindow = {
  /** `performance.now()` when it started matching. */
  on: number;
  /** `performance.now()` when it stopped, or `null` while it still matches. */
  off: number | null;
};

/**
 * Record, INSIDE THE PAGE, every period during which `selector` matches
 * something.
 *
 * WHY IT HAS TO BE IN-PAGE. Three tests in `behavior.spec.ts` assert a
 * duration — "the warning shows for ~3s", "the second pulse gets its own 2s
 * window" — and all three measured it with `Date.now()` on the Playwright side
 * plus `waitForTimeout`. That clock runs on the RUNNER, so every CDP round trip
 * and every scheduling delay is charged against a window the app is measuring
 * with its own `setTimeout`. #354 filed those as flaky under load, and they
 * are; but the worse half is that none of them actually asserted the duration
 * at all, so the window could collapse to 50 ms and every one stayed green.
 *
 * A `MutationObserver` plus `performance.now()` measures the app's timer
 * against the app's own clock. Load delays both together, so the RATIO this
 * asserts does not move, and the number it reports is the one a reader
 * experiences rather than the one the harness observed.
 *
 * `attributes: true` is not optional here: `.highlighted` goes on and off as a
 * CLASS on an element that is never added or removed, and a childList-only
 * observer sees none of it.
 *
 * WHAT IT RECORDS, AND WHAT IT DOES NOT:
 *   - It records whether the selector MATCHES, not whether anything is on
 *     screen. `display: none`, `visibility: hidden` and `opacity: 0` all leave
 *     a window open. A caller asserting that a reader SAW something must check
 *     that separately — `behavior.spec.ts`'s nudge test snapshots
 *     `visibility` / `display` / `opacity` / width in-page for exactly this
 *     reason, after both hidden shapes were planted and passed.
 *   - `attributeFilter: ["class", "style"]` is right for the two selectors in
 *     this repo (`.hero-nudge` is added and removed; `.highlighted` is a class
 *     on an element that persists). It is a TRAP for a future caller keying on
 *     `[hidden]`, `[open]`, `[aria-expanded]`, a `data-*` attribute or `id` —
 *     widen the filter with the caller.
 *   - `sample()` runs once per mutation BATCH, so an off-then-on pair inside a
 *     single React commit would be recorded as ONE continuous window rather
 *     than two. Measured for the one caller that depends on the distinction:
 *     `flashExisting` separates its two dispatches by a 10 ms `setTimeout`, so
 *     they land in different tasks and two windows are recorded (verified by
 *     firing it twice 50 ms apart). A missed pair shortens the window count,
 *     which fails the assertions rather than passing them.
 *   - The observer is never disconnected. It costs one `querySelector` per
 *     mutation for the rest of the test, which is negligible while the windows
 *     under test are open and no stream is running.
 *
 * Call before the action that opens the window; read back with
 * `readPresenceWindows`.
 */
export async function watchPresence(page: Page, key: string, selector: string) {
  await page.evaluate(
    ([k, sel]) => {
      const store = ((window as any).__cvPresence ??= {} as Record<string, PresenceWindow[]>);
      const windows: { on: number; off: number | null }[] = [];
      store[k] = windows;
      const sample = () => {
        const present = !!document.querySelector(sel);
        const open = windows.length ? windows[windows.length - 1] : null;
        if (present && (!open || open.off !== null)) {
          windows.push({ on: performance.now(), off: null });
        } else if (!present && open && open.off === null) {
          open.off = performance.now();
        }
      };
      sample();
      const observer = new MutationObserver(sample);
      observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["class", "style"],
      });
      ((window as any).__cvPresenceObservers ??= {})[k] = observer;
    },
    [key, selector] as const,
  );
}

/** Read back what `watchPresence` recorded under `key`. */
export function readPresenceWindows(page: Page, key: string): Promise<PresenceWindow[]> {
  return page.evaluate(
    (k) => ((window as any).__cvPresence?.[k] ?? []) as PresenceWindow[],
    key,
  );
}

/**
 * Block until the page's OWN clock says `ms` have passed since the start of the
 * `index`-th recorded window for `key`.
 *
 * `page.waitForTimeout(ms)` measures from whenever the runner got round to it;
 * this measures from the event, in the same clock the app's `setTimeout` uses.
 */
export async function waitSincePresence(
  page: Page,
  key: string,
  ms: number,
  index = 0,
  timeout = 15000,
) {
  await page.waitForFunction(
    ([k, target, i]) => {
      const w = (window as any).__cvPresence?.[k as string]?.[i as number];
      return !!w && performance.now() - w.on >= (target as number);
    },
    [key, ms, index] as const,
    { timeout },
  );
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
  // No `minStrongPixels` option. It was accepted and never read — the floor is
  // applied by the CALLER (`focus-ring.spec.ts`'s `s.pixels.strongPixels < 8`),
  // because that is where the failure message is built. Passing one would have
  // done nothing, silently, which is worse than not offering it; tsc only said
  // so once it could see this file (#366).
  { pad = 8, minChangedPixels = 12 }: { pad?: number; minChangedPixels?: number } = {},
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
  // `strongPixels: 0`, like the `minChangedPixels` early return above. It was
  // omitted, so this branch returned an object the declared type says it cannot
  // — invisible until #366 let tsc read this file.
  //
  // The branch IS reachable: `adjacent` is null when no pixel is both unchanged
  // and within 2px of a changed one, which is what the `filter: invert(0.5)` and
  // `backdrop-filter: contrast(0)` occluder probes in `focus-ring.spec.ts`
  // deliberately produce. But it was not a live defect, and both read sites were
  // checked rather than one:
  //   - `focus-ring.spec.ts:312` (`s.pixels.strongPixels < 8`) is the one that
  //     would have failed OPEN, since `undefined < 8` is false. It is shielded:
  //     line 304 sees the `ratio: null` this branch sets and `continue`s first.
  //   - `focus-ring.spec.ts:565` reads it with no ratio gate, but
  //     `expect(undefined).toBeGreaterThanOrEqual(8)` throws — fails closed.
  // The shield is incidental, so this is still worth fixing: reordering those
  // two checks would turn it into a real fail-open. `0` is also the honest
  // value — with no adjacent pixels, no pixel can be SHOWN to clear the bar.
  if (!adjacent) {
    return { changedPixels, strongPixels: 0, ratio: null, indicator: null, adjacent: null };
  }

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
  // away. The caller's floor on that count stops a couple of stray antialiased
  // pixels qualifying: a real 2px ring around even a small control contributes
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
