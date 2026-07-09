/**
 * Shared helpers + design-token constants for the CiteVyn UI test suite.
 *
 * Token RGB values are copied verbatim from the design source-of-truth
 * (`CiteVyn Landing v2.dc.html` — LIGHT/DARK var blocks). Tests assert computed
 * styles against these so any drift from the design fails the build.
 */
import { Page, expect } from "@playwright/test";

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
