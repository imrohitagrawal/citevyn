/**
 * Visual-regression suite (§5). Screenshots each major section in BOTH themes
 * and compares against committed baselines (tests/visual.spec.ts-snapshots/).
 *
 * Animated regions (auto-playing hero card, marquee) are masked so the
 * baselines stay deterministic; CSS animations are frozen via
 * `animations: "disabled"`.
 *
 * Regenerate baselines intentionally with:  npx playwright test visual.spec.ts --update-snapshots
 */
import { test, expect } from "@playwright/test";
import { gotoApp, ensureTheme, enterChat, type ThemeName } from "./helpers";

const THEMES: ThemeName[] = ["light", "dark"];

const SECTIONS: Array<{ name: string; selector: string; mask?: string[] }> = [
  { name: "hero", selector: ".hero", mask: [".hero-card"] },
  { name: "ticker", selector: ".ticker-strip" },
  { name: "personas", selector: "#who" },
  { name: "how-it-works", selector: "#how" },
  { name: "comparison", selector: "#why" },
  { name: "demo", selector: "#demo" },
  { name: "pricing", selector: "#pricing" },
  { name: "faq", selector: "#faq" },
  { name: "cta", selector: ".cta-banner" },
  { name: "footer", selector: "footer" },
];

for (const theme of THEMES) {
  test.describe(`[${theme}] visual regression`, () => {
    test.beforeEach(async ({ page }) => {
      await gotoApp(page);
      await ensureTheme(page, theme);
      await page.evaluate(() => (document as any).fonts?.ready);
    });

    for (const section of SECTIONS) {
      test(`${section.name}`, async ({ page }) => {
        const el = page.locator(section.selector).first();
        await el.scrollIntoViewIfNeeded();
        await page.waitForTimeout(250);
        await expect(el).toHaveScreenshot(`${section.name}-${theme}.png`, {
          animations: "disabled",
          mask: (section.mask ?? [".ticker-track"]).map((m) => page.locator(m)),
          maxDiffPixelRatio: 0.02,
        });
      });
    }

    // Chat view — empty state + composer (the region the initial suite never captured).
    test("chat-empty", async ({ page }) => {
      await enterChat(page);
      await page.waitForTimeout(250);
      await expect(page.locator('[data-screen-label="Chat"]')).toHaveScreenshot(`chat-empty-${theme}.png`, {
        animations: "disabled",
        maxDiffPixelRatio: 0.02,
      });
    });
  });
}
