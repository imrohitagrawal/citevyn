/**
 * Visual fidelity suite — asserts computed styles against the design
 * source-of-truth (CiteVyn Landing v2.dc.html) in BOTH light and dark themes.
 *
 * These catch the class of bug the work order §0 warns about: wrong tokens /
 * hardcoded colors that only surface in one theme (e.g. light text on the
 * yellow accent, muted headings, drifted skeleton widths).
 */
import { test, expect } from "@playwright/test";
import {
  TOKENS,
  SEMANTIC,
  gotoApp,
  ensureTheme,
  enterChat,
  highlightBackdropBrightFraction,
  resolveColor,
  type ThemeName,
} from "./helpers";

const THEMES: ThemeName[] = ["light", "dark"];

test.beforeEach(async ({ page }) => {
  await gotoApp(page);
});

for (const theme of THEMES) {
  test.describe(`[${theme}] fidelity`, () => {
    test.beforeEach(async ({ page }) => {
      await ensureTheme(page, theme);
    });

    const T = TOKENS[theme];

    test("theme flips every major section background to the page token", async ({ page }) => {
      // #0 prime directive: one root var set flips the WHOLE page.
      const rootBg = await page.evaluate(() =>
        getComputedStyle(document.documentElement).getPropertyValue("--bg").trim()
      );
      expect(rootBg).toBe(theme === "dark" ? "#161618" : "#faf9f6");

      // §5: FAQ section, footer, pricing cards, CTA all flip (non-transparent, themed).
      for (const sel of ["#faq", "footer"]) {
        const bg = await page.locator(sel).first().evaluate((el) => getComputedStyle(el).backgroundColor);
        expect(bg, `${sel} background`).toBe(T.bg);
      }
      const cardBg = await page.locator(".pricing-card").first().evaluate((el) => getComputedStyle(el).backgroundColor);
      expect(cardBg).toBe(T.surface);
      const ctaBg = await page.locator(".cta-banner").evaluate((el) => getComputedStyle(el).backgroundColor);
      expect(ctaBg).toBe(T.ink); // inverted panel
    });

    test("every hero/section region renders on a themed page canvas (never browser-default white)", async ({ page }) => {
      // §D2 P0: the page canvas itself must be the themed --bg. reset.css set
      // `body { background: var(--surface-base) }`, but --surface-base is only
      // defined for the data-style="softly/devtools" skins — undefined for the
      // default light/dark themes, so body resolved to transparent and every
      // bg-less section (hero, personas, how, why, demo, pricing) painted on the
      // browser-default white. In dark mode that made the light --ink hero title
      // nearly invisible.
      const bodyBg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      expect(bodyBg, "page canvas (body) background").toBe(T.bg);
      expect(bodyBg).not.toBe("rgba(0, 0, 0, 0)"); // not transparent
      expect(bodyBg).not.toBe("rgb(255, 255, 255)"); // not browser-default white

      // Every major region either sets its own themed bg or inherits the page
      // canvas. Walk the ancestor chain to the first painted background and
      // assert it is a known theme token — never transparent-all-the-way-up.
      const THEMED = new Set([T.bg, T.surface, T.surface2]);
      const regions = [".hero", ".ticker-strip", "#who", "#how", "#why", "#demo", "#pricing"];
      for (const sel of regions) {
        const effective = await page.locator(sel).first().evaluate((start) => {
          let el: Element | null = start;
          while (el) {
            const bg = getComputedStyle(el).backgroundColor;
            if (bg && bg !== "rgba(0, 0, 0, 0)" && bg !== "transparent") return bg;
            el = el.parentElement;
          }
          return "rgba(0, 0, 0, 0)"; // reached <html> with nothing painted
        });
        expect(THEMED.has(effective), `${sel} effective bg "${effective}" must be a theme token`).toBe(true);
      }
    });

    test("ticker edge fades are 80px and blend to the theme --bg", async ({ page }) => {
      // The marquee fade masks (::before/::after) sit on the ticker-strip. They
      // must be 80px wide and blend to the current theme --bg (not a stale color).
      const strip = page.locator(".ticker-strip");
      for (const pseudo of ["::before", "::after"]) {
        const width = await strip.evaluate((el, p) => getComputedStyle(el, p).width, pseudo);
        expect(width, `${pseudo} width`).toBe("80px");
        const bgi = await strip.evaluate((el, p) => getComputedStyle(el, p).backgroundImage, pseudo);
        expect(bgi, `${pseudo} gradient`).toContain("linear-gradient");
        // The gradient's opaque stop is the theme --bg rgb triple.
        expect(bgi, `${pseudo} references --bg`).toContain(T.bg);
      }
    });

    test("headings resolve to --ink and beat the body text they sit above", async ({ page }) => {
      // Root-cause rule from §2A: a heading must never be darker than its body.
      const h1 = await page.locator(".hero-title").evaluate((el) => getComputedStyle(el).color);
      expect(h1).toBe(T.ink);
      for (const h of await page.locator(".section-header h2").all()) {
        expect(await h.evaluate((el) => getComputedStyle(el).color)).toBe(T.ink);
      }
      for (const h of await page.locator(".persona-card h3, .step-meta h3, .feature-card h3").all()) {
        expect(await h.evaluate((el) => getComputedStyle(el).color)).toBe(T.ink);
      }
    });

    test("body copy is --muted, mono kickers are --faint", async ({ page }) => {
      expect(await page.locator(".hero-description").evaluate((el) => getComputedStyle(el).color)).toBe(T.muted);
      expect(await page.locator(".mono-label").first().evaluate((el) => getComputedStyle(el).color)).toBe(T.faint);
    });

    test("text on the yellow --hl accent stays dark in both themes", async ({ page }) => {
      // The exact bug the current suite missed. Read --hl-ink from the token
      // itself so the assertion tracks the source of truth, not a copy.
      const hlInk = await resolveColor(page, "var(--hl-ink)");
      const onYellow = [
        ".logo-badge",
        ".ticker-tag",
        ".doc-line.highlight-line",
        ".source-badge.one",
        ".citation-chip",
        ".popular-badge",
        ".source-number",
      ];
      for (const sel of onYellow) {
        const el = page.locator(sel).first();
        await expect(el, sel).toBeVisible();
        const bg = await el.evaluate((n) => getComputedStyle(n).backgroundColor);
        const color = await el.evaluate((n) => getComputedStyle(n).color);
        expect(bg, `${sel} bg`).toBe(T.hl);
        expect(color, `${sel} text`).toBe(hlInk); // --hl-ink, #1c1b19 in both themes
      }
    });

    test("highlighter-band text is dark ink on the page, light on the inverted CTA (both themes)", async ({ page }) => {
      // §B3 Option B: highlighted words read as dark-on-yellow (real-highlighter
      // look) in dark mode too — not light --ink text bleeding over the yellow
      // band. The inverted CTA banner keeps its light --bg highlight (per design:
      // color:var(--bg) on the dark panel).
      const hlInk = await resolveColor(page, "var(--hl-ink)");
      for (const sel of [".hero-title .highlight", ".highlight-phrase"]) {
        const el = page.locator(sel).first();
        await expect(el, sel).toBeVisible();
        expect(await el.evaluate((n) => getComputedStyle(n).color), `${sel} text`)
          .toBe(hlInk); // --hl-ink, #1c1b19 in BOTH themes
      }
      const ctaHl = page.locator(".cta-banner .highlight");
      await expect(ctaHl).toBeVisible();
      expect(await ctaHl.evaluate((n) => getComputedStyle(n).color)).toBe(T.bg);
    });

    test("highlighter band actually sits BEHIND the letters, not just under them (D2.5 legibility)", async ({ page }) => {
      // §D2.5 Bug 1: B3 darkened highlighted words to --hl-ink, but the band is
      // `linear-gradient(transparent 60%, --hl 60%)` — only the bottom 40% is
      // yellow. In DARK mode the cap region of the dark letters then sits on the
      // dark page/card canvas and is half-invisible ("check,", "I don't know.").
      // Sample a pixel row through the cap region: the backdrop must be BRIGHT
      // (yellow band, or the light canvas in light mode) so dark ink is legible.
      // Dark-mode canvas (#161618/#1e1e21) is NOT bright, so a 60% band FAILS in
      // dark. Light mode is unaffected (light canvas is already bright) — the fix
      // must not change light-mode appearance.
      for (const sel of [".hero-title .highlight", "#why .highlight", ".highlight-phrase"]) {
        const el = page.locator(sel).first();
        await expect(el, sel).toBeVisible();
        const bright = await highlightBackdropBrightFraction(el);
        expect(
          bright,
          `${sel} [${theme}] cap-row bright fraction (dark ink needs a bright backdrop)`
        ).toBeGreaterThan(0.5);
      }
    });

    test("hero highlighter is a yellow linear-gradient underlay", async ({ page }) => {
      const bgi = await page.locator(".highlight").first().evaluate((el) => getComputedStyle(el).backgroundImage);
      expect(bgi).toContain("linear-gradient");
      expect(bgi).toContain(T.hl.replace("rgb", "rgb")); // contains the --hl rgb triple
    });

    test("status + AUTO dots and step checks use semantic green", async ({ page }) => {
      expect(await page.locator(".status-dot").evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(SEMANTIC.success);
      for (const c of await page.locator(".check-icon").all()) {
        expect(await c.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(SEMANTIC.success);
      }
    });

    test("how-it-works step 01 caret: 2x15 ink bar, blinking, pinned right", async ({ page }) => {
      const caret = page.locator(".typing-caret");
      await expect(caret).toBeVisible();
      const box = await caret.evaluate((el) => {
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const pr = (el.parentElement as HTMLElement).getBoundingClientRect();
        const ps = getComputedStyle(el.parentElement as HTMLElement);
        return {
          w: cs.width, h: cs.height, bg: cs.backgroundColor, anim: cs.animationName,
          right: r.right, parentRight: pr.right, parentPadRight: parseFloat(ps.paddingRight),
        };
      });
      expect(box.w).toBe("2px");
      expect(box.h).toBe("15px");
      expect(box.bg).toBe(T.ink);
      expect(box.anim).toBe("cv-blink");
      // caret sits against the inner right edge (within a few px)
      const innerRight = box.parentRight - box.parentPadRight;
      expect(Math.abs(box.right - innerRight)).toBeLessThan(6);
    });

    test("how-it-works step 02 skeleton widths are 100 / 84 / 66 with a yellow highlight line", async ({ page }) => {
      const lines = page.locator(".doc-skeleton .doc-line");
      await expect(lines).toHaveCount(4);
      const pct = async (i: number) =>
        lines.nth(i).evaluate((el) => Math.round((el.getBoundingClientRect().width / (el.parentElement as HTMLElement).getBoundingClientRect().width) * 100));
      expect(await pct(0)).toBe(100);
      expect(await pct(1)).toBe(84);
      expect(await pct(3)).toBe(66);
      const hl = page.locator(".doc-line.highlight-line");
      expect(await hl.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(T.hl);
      expect(await hl.evaluate((el) => getComputedStyle(el).width)).not.toBe(
        await lines.nth(0).evaluate((el) => getComputedStyle(el).width)
      ); // fit-content, not a full bar
    });

    test("how-it-works highlight-line is tall enough to hold its text, not a 7px sliver (D2.5)", async ({ page }) => {
      // §D2.5 Bug 2: `.doc-line.highlight-line` inherits `height: 7px` from the
      // `.doc-line` skeleton-bar rule, so the yellow box is a sliver and its text
      // ("Use --model to pick a model per run.") overflows below it. The box must
      // be at least as tall as its font-size so it actually contains the text.
      const hl = page.locator(".doc-line.highlight-line");
      const { h, fs } = await hl.evaluate((el) => ({
        h: el.getBoundingClientRect().height,
        fs: parseFloat(getComputedStyle(el).fontSize),
      }));
      expect(h, `highlight-line height ${h}px must be ≥ font-size ${fs}px`).toBeGreaterThanOrEqual(fs);

      // The other three grey skeleton bars must stay 7px (the height override is
      // scoped to .highlight-line only).
      const plain = page.locator(".doc-skeleton .doc-line:not(.highlight-line)");
      for (const bar of await plain.all()) {
        expect(await bar.evaluate((el) => getComputedStyle(el).height)).toBe("7px");
      }
    });

    test("comparison: generic card red badge/underlines, CiteVyn card ink border + yellow badge", async ({ page }) => {
      const zero = page.locator(".source-badge.zero");
      expect(await zero.evaluate((el) => getComputedStyle(el).color)).toBe(SEMANTIC.errorChip);
      // Invented claims carry a red dotted UNDERLINE, drawn via border-bottom (per source).
      const underline = page.locator(".invention").first();
      const border = await underline.evaluate((el) => {
        const cs = getComputedStyle(el);
        return `${cs.borderBottomStyle} ${cs.borderBottomColor}`;
      });
      expect(border).toContain("dotted");
      expect(border).toContain(SEMANTIC.error); // #c25b4e

      const cv = page.locator(".compare-card.citevyn");
      expect(await cv.evaluate((el) => getComputedStyle(el).borderColor)).toBe(T.ink);
      expect(await page.locator(".source-badge.one").evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(T.hl);
      // footers: ✗ red, ✓ green
      expect(await page.locator(".compare-footer.bad").evaluate((el) => getComputedStyle(el).color)).toBe(SEMANTIC.errorChip);
      expect(await page.locator(".compare-footer.good").evaluate((el) => getComputedStyle(el).color)).toBe(SEMANTIC.success);
    });

    test("stat band shows the three gate values; four feature cards render", async ({ page }) => {
      const stats = page.locator(".stat-value");
      await expect(stats).toHaveCount(3);
      expect(await stats.nth(0).innerText()).toContain("95%");
      expect(await stats.nth(1).innerText()).toContain("100%");
      expect(await stats.nth(2).innerText()).toContain("95%");
      await expect(page.locator(".feature-card")).toHaveCount(4);
    });

    test("pricing featured card: ink border, yellow POPULAR bar + badge, filled CTA", async ({ page }) => {
      const featured = page.locator(".pricing-card.featured");
      expect(await featured.evaluate((el) => getComputedStyle(el).borderColor)).toBe(T.ink);
      expect(await page.locator(".popular-bar").evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(T.hl);
      await expect(page.locator(".popular-badge")).toHaveText("POPULAR");
      const cta = featured.locator(".cta-filled");
      expect(await cta.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(T.ink);
      // prices
      const prices = await page.locator(".price").allInnerTexts();
      expect(prices).toEqual(["$0", "$12", "Custom"]);
    });

    test("FAQ: --ink questions, --muted answer + sign, one open by default", async ({ page }) => {
      const toggles = page.locator(".faq-toggle");
      await expect(toggles).toHaveCount(6);
      for (const t of await toggles.all()) {
        expect(await t.evaluate((el) => getComputedStyle(el).color)).toBe(T.ink);
      }
      const sign = page.locator(".faq-sign").first();
      expect(await sign.evaluate((el) => getComputedStyle(el).color)).toBe(T.muted);
      const answers = page.locator(".faq-answer");
      await expect(answers).toHaveCount(1); // only first open
      expect(await answers.first().evaluate((el) => getComputedStyle(el).color)).toBe(T.muted);
    });

    test("CTA panel is intentionally inverted (--ink bg, --bg heading)", async ({ page }) => {
      const panel = page.locator(".cta-banner");
      expect(await panel.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(T.ink);
      expect(await panel.locator("h2").evaluate((el) => getComputedStyle(el).color)).toBe(T.bg);
      expect(await page.locator(".cta-pill").evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(T.hl);
    });

    test("chat view: empty-state readable, composer is a full-width surface box (input not shrunk)", async ({ page }) => {
      await enterChat(page);

      // Empty state text is bright/readable (regression guard for the 'invisible text' report).
      expect(await page.locator(".empty-state h2").evaluate((el) => getComputedStyle(el).color)).toBe(T.ink);
      expect(await page.locator(".empty-state h2").evaluate((el) => getComputedStyle(el).opacity)).toBe("1");
      expect(await page.locator(".empty-state p").evaluate((el) => getComputedStyle(el).color)).toBe(T.muted);

      // Composer: one rounded --surface box with a --border-2 outline.
      const box = page.locator(".composer-box");
      await expect(box).toBeVisible();
      expect(await box.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(T.surface);
      expect(await box.evaluate((el) => getComputedStyle(el).borderColor)).toBe(T.border2);

      // Input fills the box (not squished by an inline hint) and its placeholder is themed & visible.
      const input = page.locator(".chat-input");
      const inW = await input.evaluate((el) => el.getBoundingClientRect().width);
      expect(inW).toBeGreaterThan(400);
      expect(await input.evaluate((el) => getComputedStyle(el).color)).toBe(T.ink);
      expect(await input.getAttribute("placeholder")).toContain("Ask about");
      const ph = await input.evaluate((el) => getComputedStyle(el, "::placeholder").color);
      expect(ph).toBe(T.faint);

      // Hint sits BELOW the box (not inline in the input row).
      const boxBottom = await box.evaluate((el) => el.getBoundingClientRect().bottom);
      const hintTop = await page.locator(".composer-hint").evaluate((el) => el.getBoundingClientRect().top);
      expect(hintTop).toBeGreaterThanOrEqual(boxBottom - 1);
    });

    test("required keyframes exist (blink, scroll, fadeup, pulse, shake)", async ({ page }) => {
      const names = await page.evaluate(() => {
        const found = new Set<string>();
        for (const sheet of Array.from(document.styleSheets)) {
          let rules: CSSRuleList;
          try { rules = sheet.cssRules; } catch { continue; }
          for (const r of Array.from(rules)) {
            if (r instanceof CSSKeyframesRule) found.add(r.name);
          }
        }
        return Array.from(found);
      });
      for (const kf of ["cv-blink", "cv-scroll", "cv-fadeup", "cv-pulse", "cv-shake"]) {
        expect(names, `keyframe ${kf}`).toContain(kf);
      }
    });
  });
}
