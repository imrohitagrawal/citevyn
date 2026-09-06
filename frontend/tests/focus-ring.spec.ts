/**
 * Focus-ring guard (#355).
 *
 * WHY THIS IS A PLAYWRIGHT SPEC AND NOT A UNIT TEST. `vite.config.ts` sets
 * `css: false` for vitest, so no unit test in this repo can observe ANY
 * computed style — the defect this guards was invisible to all 482 of them.
 * And a guard asserting the CSS TEXT would not have caught it either: the
 * declaration `outline: 2px solid var(--accent)` was present in reset.css the
 * whole time. It simply did not compute, because `--accent` is defined only
 * under the three unreachable `[data-style]` skins, so the `outline` shorthand
 * was invalid at computed-value time, every longhand became `unset`, and
 * `outline-style` computed to `none` — REMOVING Chrome's own default ring,
 * because an author rule outranks the UA stylesheet.
 *
 * So this asserts what the USER PERCEIVES: it drives the page with real Tab
 * presses, reads the RESOLVED `outline-style` / `outline-color` off each
 * focused control, composites the actual painted backdrop behind the ring, and
 * computes the WCAG contrast ratio. Nothing here reads a stylesheet.
 *
 * It runs under `playwright.demo-ci.config.ts`, which is a REQUIRED check.
 */
import { test, expect } from "@playwright/test";
import { gotoApp, ensureTheme, enterChat, contrastRatio, type ThemeName } from "./helpers";

/**
 * WCAG 2.4.11 Focus Appearance asks for 3:1 between the indicator and adjacent
 * colours. `--ink` measures 9.35:1 or better on every real surface in this app
 * (see docs/UI_DESIGN.md §1.1); the floor is set at the standard's number, not
 * at ours, so a future token change has room to be different without being bad.
 */
const MIN_RING_CONTRAST = 3;

/**
 * Controls that deliberately carry no outline of their own. Each is a text
 * input inside a box that shows a `:focus-within` border instead, and each is
 * asserted separately below — the exemption is not a free pass. Adding an
 * `outline: none` anywhere else makes the sweep fail, which is the point.
 */
const OUTLINE_EXEMPT_IDS = new Set(["hero-input"]);
const OUTLINE_EXEMPT_CLASSES = new Set(["chat-input"]);

type Stop = {
  id: string;
  tag: string;
  cls: string;
  label: string;
  focusVisible: boolean;
  /** True when the control sits inside the app's one inverted (--ink) panel. */
  inInverted: boolean;
  /** True when the control sits inside a modal dialog. */
  inDialog: boolean;
  outlineStyle: string;
  outlineWidth: number;
  /** Resolved through a canvas, so `color-mix()` / `color(srgb …)` normalise. */
  outlineRGBA: number[];
  outlineRaw: string;
  /** The composited paint the ring is actually drawn over. */
  backdropRGB: number[];
  backdropRaw: string;
};

/**
 * Press Tab `max` times and record what the browser focuses at each stop.
 *
 * Real key presses, not `el.focus()`: `:focus-visible` is a heuristic keyed on
 * the interaction modality, and a programmatic focus on a <button> does not
 * match it. Reading a ring off an element that is not in the state the rule
 * targets would be a test that passes for the wrong reason.
 */
async function tabWalk(page: import("@playwright/test").Page, max: number): Promise<Stop[]> {
  const stops: Stop[] = [];
  const seen = new Set<string>();
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const stop = await page.evaluate(() => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || el === document.body || el === document.documentElement) return null;

      // Normalise ANY CSS colour (including color-mix's `color(srgb …)`) to
      // 8-bit RGBA by painting it. Parsing the string forms by hand is how a
      // colour silently reads as 0,0,0 and a contrast assertion passes for the
      // wrong reason — measured: a naive rgb() regex returned null for the
      // header's color-mix background.
      const paint = (col: string): number[] => {
        const c = document.createElement("canvas");
        c.width = c.height = 1;
        const ctx = c.getContext("2d")!;
        ctx.clearRect(0, 0, 1, 1);
        // Sentinel first: canvas silently IGNORES an unparseable fillStyle and
        // keeps the previous one, which would hand back a confident wrong
        // colour. If the sentinel survives an assignment of something that is
        // not itself magenta, the parse failed.
        ctx.fillStyle = "#ff00ff";
        ctx.fillStyle = col;
        ctx.fillRect(0, 0, 1, 1);
        const d = ctx.getImageData(0, 0, 1, 1).data;
        return [d[0], d[1], d[2], d[3] / 255];
      };

      // `over(fg, bg)` — source-over compositing, so a translucent header or a
      // backdrop resolves to the colour a human eye actually receives.
      const over = (fg: number[], bg: number[]): number[] => {
        const a = fg[3];
        return [0, 1, 2].map((i) => Math.round(fg[i] * a + bg[i] * (1 - a)));
      };

      // The ring is drawn at `outline-offset: 2px`, i.e. OUTSIDE the border
      // box, so what sits behind it is the ancestor chain's paint, not the
      // control's own background. Start at the parent for exactly that reason.
      const backdropOf = (node: HTMLElement): number[] => {
        const layers: number[][] = [];
        let n: HTMLElement | null = node.parentElement;
        while (n) {
          const c = paint(getComputedStyle(n).backgroundColor);
          if (c[3] > 0) {
            layers.push(c);
            if (c[3] >= 1) break;
          }
          n = n.parentElement;
        }
        // Anything still translucent lands on the canvas the browser paints,
        // which is white unless html/body says otherwise (they do here).
        let acc = [255, 255, 255];
        for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i], acc);
        return acc;
      };

      const cs = getComputedStyle(el);
      const backdrop = backdropOf(el);
      return {
        id: el.id || "",
        tag: el.tagName,
        cls: typeof el.className === "string" ? el.className : "",
        label: (el.getAttribute("aria-label") || el.textContent || "").trim().slice(0, 40),
        focusVisible: el.matches(":focus-visible"),
        inInverted: el.closest(".cta-banner") !== null,
        inDialog: el.closest('[role="dialog"]') !== null,
        outlineStyle: cs.outlineStyle,
        outlineWidth: parseFloat(cs.outlineWidth) || 0,
        outlineRGBA: paint(cs.outlineColor),
        outlineRaw: cs.outlineColor,
        backdropRGB: backdrop,
        backdropRaw: getComputedStyle(el.parentElement ?? el).backgroundColor,
      };
    });
    // A null stop means focus left the document (headless Chromium re-enters
    // from the top on the next press). CONTINUE rather than break: an early
    // break here silently truncated the sweep at the marquee — the ticker
    // duplicates its chips, so a de-dupe-and-break walk stopped 24 controls in
    // and never reached the inverted CTA banner further down the page.
    if (stop === null) continue;
    const key = `${stop.tag}.${stop.cls}#${stop.id}|${stop.label}`;
    if (seen.has(key)) continue; // same control again — report it once
    seen.add(key);
    stops.push(stop as Stop);
  }
  return stops;
}

function isExempt(s: Stop) {
  return (
    OUTLINE_EXEMPT_IDS.has(s.id) ||
    s.cls.split(/\s+/).some((c) => OUTLINE_EXEMPT_CLASSES.has(c))
  );
}

/** Assert every keyboard stop in `stops` renders a ring a human can see. */
function assertRings(stops: Stop[], where: string) {
  const failures: string[] = [];
  let checked = 0;
  for (const s of stops) {
    if (isExempt(s)) continue;
    checked += 1;
    const who = `${where}: ${s.tag}.${s.cls || "-"} "${s.label}"`;
    if (!s.focusVisible) {
      failures.push(`${who} — Tab focused it but it does not match :focus-visible`);
      continue;
    }
    if (s.outlineStyle === "none" || s.outlineWidth < 2) {
      failures.push(
        `${who} — outline-style: ${s.outlineStyle}, outline-width: ${s.outlineWidth}px ` +
          `(no visible ring; this is #355's exact signature)`,
      );
      continue;
    }
    const ratio = contrastRatio(s.outlineRGBA, s.backdropRGB);
    if (ratio < MIN_RING_CONTRAST) {
      failures.push(
        `${who} — ring ${s.outlineRaw} on ${s.backdropRaw} measures ${ratio.toFixed(2)}:1, ` +
          `below the ${MIN_RING_CONTRAST}:1 floor`,
      );
    }
  }
  // A sweep that checked nothing is not a pass. This is the partner that proves
  // the thing being counted exists — without it, an exemption list that grew to
  // cover everything, or a tabWalk that broke on its first press, would report
  // "0 failures" and look green.
  expect(checked, `${where}: the sweep found no controls to check`).toBeGreaterThan(0);
  expect(failures, `${where}: controls with no perceivable focus ring`).toEqual([]);
}

for (const theme of ["light", "dark"] as ThemeName[]) {
  test.describe(`focus ring — ${theme} theme (#355)`, () => {
    test.beforeEach(async ({ page }) => {
      await gotoApp(page);
      await ensureTheme(page, theme);
    });

    test("every keyboard stop on the landing page renders a ring at >= 3:1", async ({ page }) => {
      const stops = await tabWalk(page, 140);
      // The landing page has well over a dozen reachable controls; a walk that
      // collapses to two or three means Tab stopped moving, not that the page
      // got simpler.
      expect(stops.length, "keyboard stops reached on the landing page").toBeGreaterThanOrEqual(20);
      // Coverage partner: the sweep must actually REACH the inverted panel near
      // the bottom of the page, or the one surface where a flat --ink ring is
      // invisible would be swept over without ever being measured.
      expect(
        stops.filter((s) => s.inInverted).map((s) => s.label),
        "the sweep must reach the inverted CTA banner",
      ).not.toEqual([]);
      assertRings(stops, `landing/${theme}`);
    });

    test("every keyboard stop on the chat screen renders a ring at >= 3:1", async ({ page }) => {
      await enterChat(page);
      const stops = await tabWalk(page, 40);
      expect(stops.length, "keyboard stops reached on the chat screen").toBeGreaterThanOrEqual(4);
      assertRings(stops, `chat/${theme}`);
    });

    test("every keyboard stop inside the sign-in dialog renders a ring at >= 3:1", async ({
      page,
    }) => {
      // The dialog is a lazy chunk on a --surface background, and the focus
      // trap (#331) keeps Tab inside it — so it is both the densest cluster of
      // controls in the app and the one the landing sweep can never reach.
      await page.locator(".account-button", { hasText: "Sign in" }).click();
      await expect(page.locator('[role="dialog"]')).toBeVisible();
      const stops = await tabWalk(page, 30);
      expect(stops.length, "keyboard stops reached inside the dialog").toBeGreaterThanOrEqual(3);
      // Partner: the trap really is holding, so these stops are the dialog's
      // controls and not the page behind it.
      const outside = stops.filter((s) => !s.inDialog).map((s) => s.label);
      expect(outside, "Tab escaped the dialog — these stops are not what this test claims").toEqual(
        [],
      );
      assertRings(stops, `auth-dialog/${theme}`);
    });

    test("the ring stays visible on the INVERTED CTA banner, where --ink alone would be 1:1", async ({
      page,
    }) => {
      // The trap this test exists for: `--ink` is the right ring on every
      // surface in the app EXCEPT `.cta-banner`, which paints `--ink` as its
      // BACKGROUND. Measured before the fix, a plain `var(--ink)` ring on
      // `.cta-pill` scored 1.00:1 in both themes — a "fix" that is invisible on
      // the one surface it is drawn over. Anything that re-points the ring at a
      // single flat token turns this red.
      const pill = page.locator(".cta-pill").first();
      await pill.scrollIntoViewIfNeeded();
      await pill.press("Tab"); // establish keyboard modality on a real element
      await pill.focus();
      const m = await pill.evaluate((el) => {
        const paint = (col: string) => {
          const c = document.createElement("canvas");
          c.width = c.height = 1;
          const ctx = c.getContext("2d")!;
          ctx.clearRect(0, 0, 1, 1);
          ctx.fillStyle = "#ff00ff";
          ctx.fillStyle = col;
          ctx.fillRect(0, 0, 1, 1);
          const d = ctx.getImageData(0, 0, 1, 1).data;
          return [d[0], d[1], d[2], d[3] / 255];
        };
        const cs = getComputedStyle(el);
        const banner = el.closest(".cta-banner") as HTMLElement;
        return {
          outlineStyle: cs.outlineStyle,
          outlineWidth: parseFloat(cs.outlineWidth) || 0,
          ring: paint(cs.outlineColor),
          bannerBg: paint(getComputedStyle(banner).backgroundColor),
          ringRaw: cs.outlineColor,
          bannerRaw: getComputedStyle(banner).backgroundColor,
        };
      });
      expect(m.outlineStyle).not.toBe("none");
      expect(m.outlineWidth).toBeGreaterThanOrEqual(2);
      const ratio = contrastRatio(m.ring, m.bannerBg.slice(0, 3));
      expect(
        ratio,
        `ring ${m.ringRaw} on the inverted banner ${m.bannerRaw} measured ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(MIN_RING_CONTRAST);
    });

    test("the two outline-exempt text inputs show a focus-within border instead", async ({
      page,
    }) => {
      // The exemption list above is only honest if the exempted controls really
      // do signal focus another way. Both boxes already declare
      // `transition: border-color`, so the treatment was always intended.
      const border = (box: string) =>
        page.locator(box).first().evaluate((b) => getComputedStyle(b).borderTopColor);
      const ink = async () =>
        page.evaluate(() => {
          const probe = document.createElement("span");
          probe.style.color = "var(--ink)";
          document.body.appendChild(probe);
          const c = getComputedStyle(probe).color;
          probe.remove();
          return c;
        });

      // Both boxes declare `transition: border-color`, so a single read taken
      // right after `.focus()` lands MID-TRANSITION and returns the start
      // colour — a false red the first time this was written. Poll the settled
      // value instead.
      const check = async (box: string, input: string, what: string) => {
        const inkColor = await ink();
        const before = await border(box);
        // The partner. Without it, a box whose resting border ALREADY equalled
        // --ink would satisfy every assertion below while signalling nothing.
        expect(before, `${what}: resting border must differ from --ink`).not.toBe(inkColor);
        await page.locator(input).first().focus();
        // Poll for the TARGET, not merely for "changed": polling for `!= before`
        // returns on the transition's first frame and hands back an
        // intermediate colour (measured rgb(134,134,135) between --border-2 and
        // --ink), which then fails the equality below for the wrong reason.
        await expect
          .poll(() => border(box), { message: `${what} must take the --ink focus border` })
          .toBe(inkColor);
        await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
        // The reverse direction too: a rule that painted --ink unconditionally
        // would satisfy the assertion above and still signal nothing.
        await expect
          .poll(() => border(box), { message: `${what} must revert on blur` })
          .toBe(before);
      };

      await check(".hero-input-box", "#hero-input", "the hero box");

      await enterChat(page);
      // The composer is focused on mount (#302), so blur first or `before` is
      // already the focused colour for a reason unrelated to the rule.
      await page.evaluate(() => (document.activeElement as HTMLElement)?.blur());
      await page.waitForTimeout(300);
      await check(".composer-box", ".chat-input", "the composer");
    });
  });
}
