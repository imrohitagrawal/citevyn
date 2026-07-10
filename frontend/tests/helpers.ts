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

/** Wait until no streaming caret is present (streaming finished everywhere). */
export async function waitStreamDone(page: Page, timeout = 12000) {
  await page.waitForFunction(() => !document.querySelector(".typing-cursor"), {
    timeout,
  });
}
