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
 *
 * WHAT THIS SWEEP DOES NOT COVER, stated so its green is not read as more than
 * it is. It walks the landing page, the chat screen (with an answer rendered)
 * and the sign-in dialog. It does NOT open `HistoryDrawer`,
 * `ConnectedAccountsDrawer`, `Nudge`, `AccountMenu`'s popup or `ToastHost`.
 * The first four need a signed-in session; `ToastHost` does NOT — an earlier
 * version of this note said it did, and that was the wrong mechanism.
 * `ToastHost` is unreachable here because it only renders once a toast exists,
 * and in DEMO mode nothing raises one: `handleApiError` needs a live request,
 * and `park()` runs only under `if (inFlight.current)`, which `markInFlight`
 * sets from `sendLive` alone. So the restored parked-question toast, its
 * `overflowWrap`, the identical-toast collapse and the 3-card cap have NO
 * coverage in this REQUIRED job — structurally the same demo-mode blind spot
 * this file's own history is about. They are covered by vitest instead
 * (`useToast.test.tsx`, `useLandingState.test.tsx`) and the gap is recorded in
 * docs/BACKLOG.md rather than left to be rediscovered.
 *
 * The four signed-in surfaces were measured by hand during review (3-8 stops
 * each, worst ratio 13.07:1, both themes) and they inherit the same global
 * rule, so the exposure there is a FUTURE control setting `outline: none`. It
 * also cannot see `/about`, which is served by the API and carries its own copy
 * of the tokens in `frontend/public/about.css` (measured 14.43-17.21:1 by hand,
 * in agreement with the SPA).
 */
import { test, expect } from "./fixtures";
import {
  gotoApp,
  ensureTheme,
  enterChat,
  waitStreamDone,
  contrastRatio,
  measureFocusIndicator,
  freezeAnimations,
  type ThemeName,
} from "./helpers";

/**
 * WCAG 2.4.11 Focus Appearance asks for 3:1 between the indicator and adjacent
 * colours. `--ink` measures 9.35:1 or better on every surface a ring is actually
 * drawn over in this app (see docs/UI_DESIGN.md §1.1 — NOT every token: --ink on
 * --hl is 1.41:1, and the ring is never drawn over --hl because `outline-offset`
 * puts it on the parent). The floor is set at the standard's number, not at
 * ours, so a future token change has room to be different without being bad.
 */
const MIN_RING_CONTRAST = 3;

/**
 * Controls that deliberately carry no outline of their own. Each is a text
 * input inside a box that shows a `:focus-within` border instead, and each is
 * asserted separately below — the exemption is not a free pass. Adding an
 * `outline: none` anywhere else makes the sweep fail, which is the point.
 *
 * A SELECTOR, not a set of class tokens. The first version matched any element
 * carrying `chat-input` among its classes, so a future control that happened to
 * share that token — a second input, a styled wrapper — would have been
 * exempted wholesale and never measured. `matches()` pins the element itself.
 */
const OUTLINE_EXEMPT_SELECTOR = "#hero-input, input.chat-input";

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
  /** This exact ELEMENT was already visited earlier in the walk. */
  seenBefore: boolean;
  /** Matches OUTLINE_EXEMPT_SELECTOR — decided in the page, on the element. */
  exempt: boolean;
  /**
   * What focusing this control actually CHANGED on screen, from a screenshot
   * diff. This is the source of truth for whether a ring is visible; the
   * computed-style fields above are kept because they give a precise message
   * when the cause is a missing `outline`, but they cannot see occlusion.
   */
  pixels: {
    changedPixels: number;
    strongPixels: number;
    ratio: number | null;
    indicator: number[] | null;
    adjacent: number[] | null;
  } | null;
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
  // Freeze animation BEFORE any measurement: the before/after screenshot pair
  // must differ only by the focus state, and the marquee otherwise scrolls
  // between the two frames.
  await freezeAnimations(page);
  // Clear any stamps a previous walk in this same page left behind, so two
  // sweeps in one test do not silently measure nothing the second time.
  await page.evaluate(() =>
    document.querySelectorAll("[data-fr-seen]").forEach((n) => n.removeAttribute("data-fr-seen")),
  );
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const stop = await page.evaluate((EXEMPT_SELECTOR) => {
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
        // Sentinel: canvas silently IGNORES an unparseable fillStyle and keeps
        // the previous one, which would hand back a confident wrong colour.
        // The first version of this only SET the sentinel and never compared
        // against it — a comment standing in for a check, which is worse than
        // nothing here: an unparseable colour would have come back magenta,
        // and magenta measures 5.77:1 against the dark --bg, so it would have
        // PASSED the 3:1 floor. Now it throws.
        // TWO sentinels, not one. Canvas silently IGNORES an unparseable
        // fillStyle and keeps the previous value, so the check is "does the
        // result depend on what was there before?" — if it does, the assignment
        // did not take.
        //
        // The single-sentinel version had a dead escape hatch: it compared the
        // INPUT against /^#ff00ff$/i, but `getComputedStyle` never returns hex
        // — Chromium hands back `rgb(255, 0, 255)` for `magenta`, `fuchsia` and
        // `#ff00ff` alike. So a magenta focus ring (a common high-visibility
        // choice) or any magenta ancestor background would have thrown inside
        // `page.evaluate` and killed the ENTIRE sweep, not one stop.
        const read = (sentinel: string) => {
          ctx.fillStyle = sentinel;
          ctx.fillStyle = col;
          ctx.fillRect(0, 0, 1, 1);
          const d = ctx.getImageData(0, 0, 1, 1).data;
          return [d[0], d[1], d[2], d[3] / 255];
        };
        const a = read("#ff00ff");
        ctx.clearRect(0, 0, 1, 1);
        const b = read("#00ff00");
        if (a.join() !== b.join()) {
          throw new Error(`focus-ring guard: canvas could not parse the colour ${col}`);
        }
        return a;
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
      // The composited ancestor background, kept ONLY for the failure message
      // (naming what the ring was drawn over is useful when a stop fails). It
      // is deliberately NOT the verdict any more: two rounds of keying on
      // computed colours reported 16.35:1 for rings that screenshots measured
      // at 1.00:1, because a proxy cannot see occlusion, filters or opacity.
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
        let acc = [255, 255, 255];
        for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i], acc);
        return acc;
      };

      const cs = getComputedStyle(el);
      const backdrop = backdropOf(el);
      // TRUE identity, stamped on the element itself. The first version keyed
      // de-duplication on `tag.class#id|label`, which is not an identity: a
      // reviewer put a SECOND `.cta-pill` with `outline: none` next to the real
      // one, and because the twin collapsed onto the same key it was never
      // measured and the sweep stayed green on a genuinely broken control.
      const seenBefore = el.hasAttribute("data-fr-seen");
      el.setAttribute("data-fr-seen", "");
      return {
        seenBefore,
        exempt: el.matches(EXEMPT_SELECTOR),
        pixels: null,
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
    }, OUTLINE_EXEMPT_SELECTOR);
    // A null stop means focus left the document (headless Chromium re-enters
    // from the top on the next press). CONTINUE rather than break: an early
    // break here silently truncated the sweep at the marquee — the ticker
    // duplicates its chips, so a de-dupe-and-break walk stopped 24 controls in
    // and never reached the inverted CTA banner further down the page.
    if (stop === null) continue;
    if (stop.seenBefore) continue; // this exact element again — report it once
    // The verdict: what focusing actually changed on screen. Taken here, while
    // this control is the one the browser has focused from a real Tab.
    (stop as Stop).pixels = await measureFocusIndicator(page);
    stops.push(stop as Stop);
  }
  await page.evaluate(() =>
    document.querySelectorAll("[data-fr-seen]").forEach((n) => n.removeAttribute("data-fr-seen")),
  );
  return stops;
}

function isExempt(s: Stop) {
  return s.exempt;
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
    // THE VERDICT IS THE PIXELS. Everything above is a computed-style
    // convenience that gives a precise message for the common cause; it cannot
    // see an overlay, a filter or an opacity, and two earlier versions of this
    // guard passed rings that screenshots measured at 1.00:1 for exactly that
    // reason.
    if (s.pixels === null) {
      failures.push(`${who} — could not sample this control's pixels (no box to screenshot)`);
      continue;
    }
    if (s.pixels.changedPixels < 12) {
      failures.push(
        `${who} — focusing it changes ${s.pixels.changedPixels} pixels on screen. ` +
          `Computed style says outline ${s.outlineStyle} ${s.outlineWidth}px ${s.outlineRaw}, ` +
          `so something is painting OVER the ring (an overlay, a filter, an opacity) ` +
          `or it is drawn outside the sampled area`,
      );
      continue;
    }
    if (s.pixels.ratio === null) {
      failures.push(`${who} — the focus indicator has no adjacent pixels to compare against`);
      continue;
    }
    // At least a ring's worth of pixels must clear the floor. Counting them
    // rather than averaging is what lets a legitimately large visual change —
    // the marquee un-fading when focus enters it — coexist with a strict
    // verdict on the ring itself.
    if (s.pixels.strongPixels < 8) {
      failures.push(
        `${who} — the focus indicator's best pixel PAINTS at ` +
          `${s.pixels.ratio.toFixed(2)}:1 (rgb(${s.pixels.indicator}) vs adjacent ` +
          `rgb(${s.pixels.adjacent})), and only ${s.pixels.strongPixels} pixels reach the ` +
          `${MIN_RING_CONTRAST}:1 floor. Computed style claimed ${s.outlineRaw} on ` +
          `${s.backdropRaw}, so something is painting over or through it`,
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
      // `.cta-banner` moved into the lazily-loaded strip (#358), and the
      // coverage partner below REQUIRES the sweep to reach it. At 1280x720 the
      // observer happens to fire on first paint, so this passes today by
      // timing rather than by anything this spec states — and this job fails on
      // `flaky != 0`. Waiting for it makes the dependency explicit instead of
      // leaving a focus-ring test to red for a lazy-loading reason.
      await expect(page.locator(".cta-banner")).toBeAttached({ timeout: 15_000 });
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
      // Ask something first. An empty chat screen has four controls and NO
      // answer, so the sweep would otherwise never walk anything an answer
      // renders. Demo mode answers from the built-in KB, no backend needed.
      //
      // What this still does NOT reach: `a.citation-chip`. Chips come from `[n]`
      // markers, which only the LIVE path emits — measured in demo mode:
      // `chips=0, cards=2`. A live-only test here would self-skip, and the
      // required job pins `stats["skipped"] == 4`, so the chip's own rule is
      // covered by the injected-element test below instead.
      await page.locator(".chat-input").fill("What is Claude Code?");
      await page.keyboard.press("Enter");
      await waitStreamDone(page);
      const rendered = await page.evaluate(() => ({
        cards: document.querySelectorAll(".sources .source-card").length,
        chips: document.querySelectorAll("a.citation-chip").length,
      }));
      // Partner: proves the sweep below had a rendered answer to walk. Without
      // it, an answer that failed to render would leave the sweep measuring the
      // same four empty-screen controls and still reporting green.
      expect(rendered.cards, "the demo answer must render source cards").toBeGreaterThan(0);
      const stops = await tabWalk(page, 40);
      expect(stops.length, "keyboard stops reached on the chat screen").toBeGreaterThanOrEqual(5);
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

    // Every shape below produced a MEASURED 1.00:1 ring that earlier versions of
    // this sweep passed at a confident 16.35:1, because they composited
    // computed background colours instead of looking at the screen. They are
    // injected at runtime rather than shipped: the point is to prove the
    // measurement catches the CLASS, not to add occluders to the app.
    const OCCLUDERS: Array<{ name: string; setup: string }> = [
      {
        name: "a gradient-painted panel (background-color reads transparent)",
        setup: `panel.style.backgroundImage = "linear-gradient(var(--ink), var(--ink))";`,
      },
      {
        name: "an absolutely-positioned SIBLING drawn over the control",
        setup: `panel.style.position = "relative";
          const cover = document.createElement("div");
          cover.style.cssText =
            "position:absolute;inset:-20px;background:var(--ink);z-index:5";
          panel.appendChild(cover);`,
      },
      {
        name: "an ancestor ::before overlay",
        setup: `panel.style.position = "relative";
          const st = document.createElement("style");
          st.id = "fr-probe-style";
          st.textContent =
            "#fr-probe::before{content:'';position:absolute;inset:-20px;background:var(--ink);z-index:5}";
          document.head.appendChild(st);`,
      },
      {
        // The panel needs its OWN background, or the filter only touches the
        // panel's content and the ring still sits on an unfiltered page. That
        // distinction is measured, not assumed: with a transparent panel this
        // probe leaves 588 changed pixels and 172 distinct colours, and the
        // ring stays perceivable — so a version of this test without the
        // background would have asserted a bug that does not exist.
        name: "an ancestor filter: invert(0.5), which flattens ring and backdrop alike",
        setup: `panel.style.background = "var(--bg)";
          panel.style.filter = "invert(0.5)";`,
      },
      {
        name: "an overlay with backdrop-filter: contrast(0)",
        setup: `panel.style.background = "var(--bg)";
          panel.style.position = "relative";
          const cover = document.createElement("div");
          cover.style.cssText =
            "position:absolute;inset:-20px;backdrop-filter:contrast(0);z-index:5";
          panel.appendChild(cover);`,
      },
      {
        name: "an ancestor opacity: 0.06",
        setup: `panel.style.background = "var(--bg)";
          panel.style.opacity = "0.06";`,
      },
    ];

    for (const occluder of OCCLUDERS) {
      test(`sees through ${occluder.name}`, async ({ page }) => {
        await page.evaluate((setup) => {
          const panel = document.createElement("div");
          panel.id = "fr-probe";
          panel.style.padding = "24px";
          const btn = document.createElement("button");
          btn.className = "fr-probe-pill";
          btn.textContent = "Occluded probe";
          panel.appendChild(btn);
          document.body.appendChild(panel);
          // eslint-disable-next-line no-new-func
          new Function("panel", setup)(panel);
        }, occluder.setup);

        const stops = await tabWalk(page, 160);
        const probe = stops.filter((st) => st.cls.includes("fr-probe-pill"));
        // Partner: the sweep must actually have REACHED the probe, or the
        // assertion below would pass on an empty list.
        expect(probe.length, "the sweep must reach the injected probe").toBe(1);
        // It must FAIL — either because focusing changes nothing visible, or
        // because what it does change paints below the floor.
        expect(() => assertRings(probe, "probe")).toThrow(
          /changes \d+ pixels on screen|PAINTS at|no adjacent pixels|could not sample/,
        );

        await page.evaluate(() => {
          document.getElementById("fr-probe")?.remove();
          document.getElementById("fr-probe-style")?.remove();
        });
      });
    }

    test("and PASSES the same probe with no occluder, so it is not simply rejecting probes", async ({
      page,
    }) => {
      // The control case for the six above. Without it, a measurement that
      // failed everything would look like a perfect guard.
      await page.evaluate(() => {
        const panel = document.createElement("div");
        panel.id = "fr-probe";
        panel.style.padding = "24px";
        const btn = document.createElement("button");
        btn.className = "fr-probe-pill";
        btn.textContent = "Clear probe";
        panel.appendChild(btn);
        document.body.appendChild(panel);
      });
      const stops = await tabWalk(page, 160);
      const probe = stops.filter((st) => st.cls.includes("fr-probe-pill"));
      expect(probe.length).toBe(1);
      expect(probe[0].pixels!.changedPixels).toBeGreaterThan(12);
      expect(probe[0].pixels!.strongPixels).toBeGreaterThanOrEqual(8);
      expect(probe[0].pixels!.ratio!).toBeGreaterThanOrEqual(MIN_RING_CONTRAST);
      expect(() => assertRings(probe, "probe")).not.toThrow();
      await page.evaluate(() => document.getElementById("fr-probe")?.remove());
    });

    test("a citation chip's ring follows --focus-ring, not a hardcoded --ink", async ({
      page,
    }) => {
      // `a.citation-chip:focus-visible` has its OWN declaration at a specificity
      // the shared rule cannot reach, so it used to hardcode `var(--ink)` and
      // silently opt out of the token. Today that is invisible — outside
      // `.cta-banner` the two resolve to the same colour — which is exactly why
      // it needs proving rather than eyeballing: re-point `--focus-ring` on an
      // ancestor and the chip's ring must follow, or a chip inside a future
      // inverted surface is a 1.00:1 ring again.
      //
      // The chip is INJECTED rather than driven through the app: chips are
      // rendered from `[n]` markers, which only the live path emits (measured in
      // demo mode: chips=0, cards=2), and a live-only test would self-skip and
      // break the required job's `skipped == 4` pin. What is under test is the
      // CSS rule, and an `<a class="citation-chip">` is what that rule selects.
      await enterChat(page);
      const ring = await page.evaluate(() => {
        const host = document.querySelector("#chat-list") as HTMLElement;
        const chip = document.createElement("a");
        chip.className = "citation-chip";
        chip.href = "#";
        chip.textContent = "[1]";
        host.appendChild(chip);
        // Read the rule directly: `:focus-visible` needs keyboard modality, so
        // resolve the declared value instead of faking a focus state.
        const declared = getComputedStyle(chip).getPropertyValue("--focus-ring").trim();
        host.style.setProperty("--focus-ring", "rgb(1, 2, 3)");
        const repointed = getComputedStyle(chip).getPropertyValue("--focus-ring").trim();
        // And prove the RULE consumes it, by applying the same declaration the
        // stylesheet does and reading what it computes to.
        chip.style.outline = "2px solid var(--focus-ring)";
        const outline = getComputedStyle(chip).outlineColor;
        host.style.removeProperty("--focus-ring");
        chip.remove();
        return { declared, repointed, outline };
      });
      // Partner: the token really was something else first, so the change below
      // is a change and not a coincidence.
      expect(ring.declared).not.toBe("rgb(1, 2, 3)");
      expect(ring.repointed).toBe("rgb(1, 2, 3)");
      expect(ring.outline, "the chip's ring must resolve through --focus-ring").toBe(
        "rgb(1, 2, 3)",
      );
      // And the stylesheet rule itself must be the token, not a hardcoded --ink.
      const rule = await page.evaluate(() => {
        for (const sheet of Array.from(document.styleSheets)) {
          let rules: CSSRuleList;
          try {
            rules = sheet.cssRules;
          } catch {
            continue; // cross-origin sheet
          }
          for (const r of Array.from(rules)) {
            if (r instanceof CSSStyleRule && r.selectorText === "a.citation-chip:focus-visible") {
              return r.style.getPropertyValue("outline");
            }
          }
        }
        return null;
      });
      expect(rule, "a.citation-chip:focus-visible must exist as its own rule").not.toBeNull();
      expect(rule).toContain("var(--focus-ring)");
    });

    test("a control that merely SHARES an exempt class token is still measured", async ({
      page,
    }) => {
      // The exemption used to match any element carrying `chat-input` among its
      // classes, so a future control sharing that token would have been
      // exempted wholesale and never measured. It is a selector now, and this
      // is its bite-proof — reinstating the token match makes the probe below
      // invisible and the sweep green.
      await page.evaluate(() => {
        const btn = document.createElement("button");
        // Shares the token, is NOT the exempt control.
        btn.className = "chat-input fr-token-probe";
        btn.style.outline = "none";
        btn.textContent = "Token probe";
        document.body.appendChild(btn);
      });
      const stops = await tabWalk(page, 160);
      const probe = stops.filter((st) => st.cls.includes("fr-token-probe"));
      expect(probe.length, "the sweep must reach the probe").toBe(1);
      expect(probe[0].exempt, "sharing a class token must NOT exempt a control").toBe(false);
      expect(() => assertRings(probe, "token")).toThrow();
      await page.evaluate(() => document.querySelector(".fr-token-probe")?.remove());
    });

    test("two sweeps in one page both measure the whole page", async ({ page }) => {
      // The identity stamps must be cleared between walks. Without it the
      // second sweep in a test would skip every control it had already seen and
      // report green having measured nothing — a vacuous pass.
      const first = await tabWalk(page, 140);
      const second = await tabWalk(page, 140);
      expect(first.length).toBeGreaterThanOrEqual(20);
      expect(second.length, "the second sweep must not be silently empty").toBe(first.length);
    });

    test("the canvas colour reader rejects a colour it cannot parse", async ({ page }) => {
      // The sweep normalises colours through a canvas, and canvas silently
      // IGNORES an unparseable fillStyle, keeping the previous value — which
      // would hand back a confident WRONG colour. Two sentinels catch that: if
      // the answer depends on what was there before, the assignment did not
      // take.
      //
      // This asserts the expression the sweep embeds, in BOTH directions,
      // because no colour `getComputedStyle` can return will ever trigger it in
      // a real walk. The earlier single-sentinel version compared the INPUT
      // against /^#ff00ff$/i and was dead code: Chromium returns
      // `rgb(255, 0, 255)` for `magenta`, `fuchsia` and `#ff00ff` alike, so a
      // magenta ring would have thrown and killed the entire sweep.
      const r = await page.evaluate(() => {
        const probe = (col: string) => {
          const c = document.createElement("canvas");
          c.width = c.height = 1;
          const ctx = c.getContext("2d")!;
          const read = (sentinel: string) => {
            ctx.fillStyle = sentinel;
            ctx.fillStyle = col;
            ctx.fillRect(0, 0, 1, 1);
            const d = ctx.getImageData(0, 0, 1, 1).data;
            return [d[0], d[1], d[2], d[3] / 255];
          };
          const a = read("#ff00ff");
          ctx.clearRect(0, 0, 1, 1);
          const b = read("#00ff00");
          return { parsed: a.join() === b.join(), value: a };
        };
        return {
          magenta: probe("rgb(255, 0, 255)"),
          named: probe("magenta"),
          srgb: probe("color(srgb 1 0 1)"),
          ink: probe("rgb(28, 27, 25)"),
          garbage: probe("not-a-colour"),
        };
      });
      // Real colours parse — including the three magenta spellings that the
      // dead escape hatch would have thrown on.
      expect(r.magenta.parsed).toBe(true);
      expect(r.named.parsed).toBe(true);
      expect(r.srgb.parsed).toBe(true);
      expect(r.ink.parsed).toBe(true);
      expect(r.ink.value.slice(0, 3)).toEqual([28, 27, 25]);
      // And an unparseable one is caught rather than silently read as whatever
      // was in the context before it.
      expect(r.garbage.parsed).toBe(false);
    });

    test("a broken TWIN of a compliant control is still measured", async ({ page }) => {
      // The bite-proof for element identity. De-duplication used to key on
      // `tag.class#id|label`, which is not an identity: a reviewer put a second
      // `.cta-pill` with `outline: none` beside the real one, the twin
      // collapsed onto the same key, was never measured, and the sweep reported
      // 10/10 green on a genuinely unringed, keyboard-reachable control.
      await page.evaluate(() => {
        const real = document.querySelector(".cta-pill") as HTMLElement;
        const twin = real.cloneNode(true) as HTMLElement;
        twin.style.outline = "none";
        twin.setAttribute("data-fr-twin", "");
        real.after(twin);
      });
      const stops = await tabWalk(page, 140);
      const pills = stops.filter((s) => s.cls.includes("cta-pill"));
      // Partner: BOTH must be walked, or the assertion below is vacuous.
      expect(pills.length, "both the real pill and its twin must be measured").toBe(2);
      expect(() => assertRings(stops, "twin")).toThrow(/no visible ring/);
      await page.evaluate(() => document.querySelector("[data-fr-twin]")?.remove());
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
