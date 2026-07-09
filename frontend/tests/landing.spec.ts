/**
 * Playwright test suite for CiteVyn Landing Page
 * Tests theming, navigation, chat, and interactive components in both light/dark modes.
 */
import { test, expect } from "@playwright/test";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const DARK_INK = "rgb(240, 239, 233)"; // #f0efe9 — --ink in dark mode

function hexToRgb(hex: string): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgb(${r}, ${g}, ${b})`;
}

// Contrast ratio: simplified WCAG formula for text on background
function contrastRatio(c1: [number, number, number], c2: [number, number, number]): number {
  const lum = (c: [number, number, number]) => {
    const [r, g, b] = c.map(v => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const L1 = lum(c1);
  const L2 = lum(c2);
  const lighter = Math.max(L1, L2);
  const darker = Math.min(L1, L2);
  return (lighter + 0.05) / (darker + 0.05);
}

function parseRgb(rgb: string): [number, number, number] {
  const m = rgb.match(/rgb\((\d+),\s*(\d+),\s*(\d+)\)/);
  if (!m) return [0, 0, 0];
  return [parseInt(m[1]), parseInt(m[2]), parseInt(m[3])];
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

test.describe("CiteVyn Landing Page", () => {
  test.beforeEach(async ({ page }) => {
    // Use "commit" (not "domcontentloaded"): on a cold Vite dev server the first
    // request triggers dependency optimization, which forces a full-page reload.
    // That reload aborts an in-flight "domcontentloaded" goto (net::ERR_ABORTED).
    // "commit" resolves as soon as navigation commits; we then wait for the app
    // to mount, tolerating any subsequent optimize-reload.
    await page.goto("/", { waitUntil: "commit" });
    await page.waitForSelector(".theme-toggle", { timeout: 30000 });
  });

  // ==========================================================================
  // THEMING
  // ==========================================================================

  test.describe("Theme", () => {
    test("dark theme flips the whole page", async ({ page }) => {
      const themeBtn = page.locator(".theme-toggle");
      await expect(themeBtn).toBeVisible();

      // Read background color before toggle (light mode)
      const beforeBg = await page.evaluate(() => {
        return getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
      });

      // Toggle to dark
      await themeBtn.click();
      await page.waitForTimeout(300);

      // CSS vars must have changed
      const afterBg = await page.evaluate(() => {
        return getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
      });

      // --bg token must differ between light and dark
      // Light: #faf9f6, Dark: #161618
      expect(afterBg).not.toBe(beforeBg);
      expect(afterBg).toBe("#161618");

      // Check a section's computed background changed
      const faqBg = await page.locator("#faq").evaluate(
        el => getComputedStyle(el).backgroundColor
      );
      const footerBg = await page.locator("footer").evaluate(
        el => getComputedStyle(el).backgroundColor
      );
      expect(faqBg).not.toBe("rgba(0, 0, 0, 0)");
      expect(footerBg).not.toBe("rgba(0, 0, 0, 0)");
    });

    test("dark theme heading contrast", async ({ page }) => {
      // Toggle dark
      const themeBtn = page.locator(".theme-toggle");
      await themeBtn.click();
      // Poll the real condition (root --bg flipped to the dark token) rather than
      // a fixed sleep that can read before React has applied data-theme under load.
      await expect
        .poll(async () =>
          page.evaluate(() =>
            getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()
          )
        )
        .toBe("#161618");

      // Get dark background color for contrast calculation
      const bgColor = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      const bgRgb = parseRgb(bgColor);

      // Check every h1 and h2 — excluding the CTA panel, which is intentionally
      // inverted (--ink background, --bg heading text) per the design spec.
      const headings = await page.locator("h1, h2").all();
      for (const h of headings) {
        const inInvertedPanel = await h.evaluate((el) => !!el.closest(".cta-banner"));
        if (inInvertedPanel) continue;
        await expect(h).toBeVisible();
        const color = await h.evaluate(el => getComputedStyle(el).color);
        expect(color).toBe(DARK_INK);

        // WCAG contrast >= 4.5 for normal text
        const fgRgb = parseRgb(color);
        const ratio = contrastRatio(fgRgb, bgRgb);
        expect(ratio).toBeGreaterThanOrEqual(4.5);
      }

      // FAQ question labels (they are buttons styled as headings)
      const faqBtns = page.locator(".faq-toggle");
      const count = await faqBtns.count();
      for (let i = 0; i < count; i++) {
        const btn = faqBtns.nth(i);
        await expect(btn).toBeVisible();
        const color = await btn.evaluate(el => getComputedStyle(el).color);
        expect(color).toBe(DARK_INK);
      }
    });

    test("toggle label shows target mode", async ({ page }) => {
      const themeBtn = page.locator(".theme-toggle");

      // Light mode: label shows "DARK"
      await expect(themeBtn).toContainText("DARK");

      // Toggle: goes to dark
      await themeBtn.click();
      await page.waitForTimeout(200);
      await expect(themeBtn).toContainText("LIGHT");

      // Toggle back: goes to light
      await themeBtn.click();
      await page.waitForTimeout(200);
      await expect(themeBtn).toContainText("DARK");
    });
  });

  // ==========================================================================
  // NAVIGATION
  // ==========================================================================

  test.describe("Navigation", () => {
    test("nav links scroll to sections with header offset", async ({ page }) => {
      // Who it's for link
      const whoLink = page.locator('.nav-link', { hasText: "Who" });
      await whoLink.click();

      // Wait for smooth scroll to complete
      await page.waitForFunction(
        () => {
          const el = document.getElementById("who");
          if (!el) return false;
          const rect = el.getBoundingClientRect();
          return rect.top >= 64 && rect.top <= 100;
        },
        { timeout: 5000 }
      ).catch(() => {});

      const whoSection = page.locator("#who");
      const rect = await whoSection.boundingBox();
      expect(rect).not.toBeNull();
      // boundingBox() reports viewport-relative coordinates as { x, y, width, height }
      // (there is no `.top`). Account for the sticky header (~64px) + 72px scroll
      // offset — the section should land just below the header.
      expect(rect!.y).toBeLessThan(120);
      expect(rect!.y).toBeGreaterThan(60);
    });

    test("nav links work from chat view", async ({ page }) => {
      // Enter chat via Try the demo
      await page.locator(".cta-button", { hasText: "Try the demo" }).click();
      await page.waitForTimeout(400);

      // Should be on chat screen
      await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();

      // Click nav link (e.g. "FAQ")
      const faqLink = page.locator('.nav-link', { hasText: "FAQ" });
      await faqLink.click();
      await page.waitForTimeout(800);

      // Should be back on landing — FAQ section should be visible
      const faqSection = page.locator("#faq");
      await expect(faqSection).toBeVisible();
    });

    test("CTAs enter chat, not navigate", async ({ page }) => {
      const urlBefore = page.url();

      // "Try the demo" button
      await page.locator(".cta-button", { hasText: "Try the demo" }).click();
      await page.waitForTimeout(300);
      await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
      expect(page.url()).toBe(urlBefore); // no navigation

      // Back
      await page.locator(".back-button").click();
      await page.waitForTimeout(300);

      // Hero Ask with text
      await page.locator("#hero-input").fill("What is Claude Code?");
      await page.locator(".ask-button").click();
      await page.waitForTimeout(300);
      await expect(page.locator('[data-screen-label="Chat"]')).toBeVisible();
      expect(page.url()).toBe(urlBefore);
    });
  });

  // ==========================================================================
  // HERO
  // ==========================================================================

  test.describe("Hero", () => {
    test("empty ask does not navigate", async ({ page }) => {
      // Click Ask with empty box — stays on landing
      await page.locator(".ask-button").click();
      await page.waitForTimeout(300);

      // Should still be on landing
      await expect(page.locator(".hero-input-box")).toBeVisible();

      // Border should turn amber (shake + warning)
      await expect(page.locator(".hero-nudge")).toBeVisible();

      // Warning disappears after ~3s
      await page.waitForTimeout(3300);
      await expect(page.locator(".hero-nudge")).not.toBeVisible();
    });

    test("slash focuses hero input, ignored inside inputs", async ({ page }) => {
      const heroInput = page.locator("#hero-input");

      // Focus a different element first
      await page.locator(".cta-button").focus();

      // Press / — should focus hero input
      await page.keyboard.press("/");
      await expect(heroInput).toBeFocused();

      // Inside an input, / should not trigger focus
      await heroInput.fill("test question");
      await page.keyboard.press("/");
      // Input still focused (value unchanged — no '/' added since / was consumed)
      await expect(heroInput).toBeFocused();
    });
  });

  // ==========================================================================
  // CHAT
  // ==========================================================================

  test.describe("Chat", () => {
    test("question streams answer then sources", async ({ page }) => {
      // Enter chat
      await page.locator(".cta-button", { hasText: "Try the demo" }).click();
      await page.waitForTimeout(300);

      // Ask a question
      const chatInput = page.locator(".chat-input");
      await chatInput.fill("How does Claude Code work?");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(300);

      // User bubble appears
      await expect(page.locator(".message.user-msg")).toBeVisible();

      // Wait for streaming to finish (caret disappears)
      await page.waitForFunction(
        () => !document.querySelector(".typing-cursor"),
        { timeout: 10000 }
      );

      // Source cards appear
      const sourceCards = page.locator(".source-card");
      await expect(sourceCards.first()).toBeVisible();
    });

    test("out-of-scope question shows REFUSED badge, no sources", async ({ page }) => {
      // Enter chat
      await page.locator(".cta-button", { hasText: "Try the demo" }).click();
      await page.waitForTimeout(300);

      // Ask out-of-scope question
      const chatInput = page.locator(".chat-input");
      await chatInput.fill("What is the best laptop to buy?");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(300);

      // User bubble appears
      await expect(page.locator(".message.user-msg")).toBeVisible();

      // Wait for streaming to finish
      await page.waitForFunction(
        () => !document.querySelector(".typing-cursor"),
        { timeout: 10000 }
      );

      // REFUSED badge should be visible
      await expect(page.locator(".refusal-badge, .badge-refused")).toBeVisible();

      // No source cards
      const sourceCards = page.locator(".source-card");
      expect(await sourceCards.count()).toBe(0);
    });

    test("duplicate question scrolls to original and pulses", async ({ page }) => {
      // Enter chat
      await page.locator(".cta-button", { hasText: "Try the demo" }).click();
      await page.waitForTimeout(300);

      const chatInput = page.locator(".chat-input");

      // Ask a question
      await chatInput.fill("What is Claude Code?");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(300);

      const msgCount1 = await page.locator(".message.user-msg").count();
      expect(msgCount1).toBe(1);

      // Ask the same question again
      await chatInput.fill("What is Claude Code?");
      await page.keyboard.press("Enter");
      await page.waitForTimeout(300);

      // Message count should NOT increase
      const msgCount2 = await page.locator(".message.user-msg").count();
      expect(msgCount2).toBe(msgCount1);
    });

    test("chat autoscrolls to newest message", async ({ page }) => {
      // Enter chat
      await page.locator(".cta-button", { hasText: "Try the demo" }).click();
      await page.waitForTimeout(300);

      const chatInput = page.locator(".chat-input");
      const chatList = page.locator("#chat-list");

      // Ask multiple questions
      for (const q of ["What is Claude Code?", "How does it install?", "What models does it use?"]) {
        await chatInput.fill(q);
        await page.keyboard.press("Enter");
        await page.waitForTimeout(200);
      }

      // Chat list should be scrolled to bottom (newest message visible)
      const isAtBottom = await page.evaluate(() => {
        const list = document.getElementById("chat-list");
        if (!list) return false;
        return Math.abs(list.scrollHeight - list.scrollTop - list.clientHeight) < 5;
      });
      expect(isAtBottom).toBe(true);
    });
  });

  // ==========================================================================
  // COMPONENTS
  // ==========================================================================

  test.describe("Components", () => {
    test("accordion: first open, one at a time", async ({ page }) => {
      const faqSection = page.locator("#faq");
      await faqSection.scrollIntoViewIfNeeded();
      await page.waitForTimeout(200);

      const faqItems = page.locator(".faq-item");
      const count = await faqItems.count();
      expect(count).toBeGreaterThanOrEqual(6);

      // First FAQ should be open by default (has answer visible)
      const firstAnswer = faqItems.nth(0).locator(".faq-answer");
      await expect(firstAnswer).toBeVisible();

      // Click second FAQ — first should close
      await faqItems.nth(1).locator(".faq-toggle").click();
      await page.waitForTimeout(200);

      // First should be closed now
      await expect(firstAnswer).not.toBeVisible();
      // Second should be open
      await expect(faqItems.nth(1).locator(".faq-answer")).toBeVisible();
    });

    test("demo rail: selecting sets active styling + streams", async ({ page }) => {
      const demoSection = page.locator("#demo");
      await demoSection.scrollIntoViewIfNeeded();
      await page.waitForTimeout(300);

      const demoButtons = page.locator(".demo-question");
      const count = await demoButtons.count();
      expect(count).toBeGreaterThanOrEqual(4);

      // Click second question
      await demoButtons.nth(1).click();
      await page.waitForTimeout(200);

      // Should have active class (ink border + shadow)
      const activeBtn = demoButtons.nth(1);
      await expect(activeBtn).toHaveClass(/active/);

      // Answer should appear
      await expect(page.locator(".demo-answer")).toBeVisible();
    });

    test("marquee pauses on hover", async ({ page }) => {
      const ticker = page.locator(".ticker-track");
      await expect(ticker).toBeVisible();

      // Hover the STATIONARY strip, not the track: the track is width:max-content
      // and translateX-animated, so its bounding box runs far off-screen left and
      // a position-relative hover lands nowhere. The strip is stable and the
      // track fills it, so `.ticker-track:hover` still matches and pauses it.
      const strip = page.locator(".ticker-strip");
      await strip.hover({ force: true, position: { x: 200, y: 25 } });
      await expect
        .poll(async () => ticker.evaluate(el => getComputedStyle(el).animationPlayState))
        .toBe("paused");
    });

    test("hero card auto-plays and cycles 3 QAs", async ({ page }) => {
      const cardContent = page.locator(".card-content");
      await expect(cardContent).toBeVisible();

      // Get first Q text
      const firstQ = await page.locator(".message-text").first().textContent();
      expect(firstQ).toBeTruthy();

      // Wait for streaming to finish (hero auto-plays with a streaming caret)
      // Each answer takes ~2-3s to stream, then 4.6s pause before next cycle.
      // Wait up to 10s for the caret to disappear, indicating answer is done.
      await page.waitForFunction(
        () => !document.querySelector(".typing-cursor"),
        { timeout: 12000 }
      ).catch(() => {});

      // Wait for next Q to appear (4.6s pause + small buffer = 5.5s)
      await page.waitForTimeout(5500);

      // Q should have changed to a different one
      const secondQ = await page.locator(".message-text").first().textContent();
      expect(secondQ).not.toBe(firstQ);
    });
  });
});
