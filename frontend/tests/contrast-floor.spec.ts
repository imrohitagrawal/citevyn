/**
 * #403 — a WCAG contrast FLOOR sweep, not a token ban.
 *
 * WHY THIS FILE EXISTS. #396 shipped a guard that asks "does anything render
 * `--faint`?". That is a question about one token, and seven text runs failed
 * WCAG 1.4.3 AA using OTHER tokens, so nothing caught them. The guard could not
 * see them by construction — it was never looking at contrast. This file asks
 * the question the criterion actually poses: does every text run a reader can
 * see clear the ratio its size and weight earn?
 *
 * WHAT IT MEASURED, on the rendered page, before the fix in this change
 * (light / dark; `—` means that theme already cleared the floor):
 *
 *   .refusal-badge        11px/600   3.20 / 3.24
 *   .hero-nudge           13px/500   3.68 / —
 *   .compare-footer.good  12.5px     3.60 / —
 *   span inside it        12.5px     3.60 / —
 *   .check-icon           9px        3.42 / —      (x2 — two instances)
 *   .compare-footer.bad   12.5px     —    / 3.22
 *   span inside it        12.5px     —    / 3.22
 *   .source-badge.zero    10px/700   —    / 2.92
 *   #hero-input::placeholder 16px    —    / 3.61
 *   .notice-rate-limit    11px/600   4.31 / 3.17
 *   .notice-error         11px/600   —    / 2.67
 *
 * The last two are not reachable in demo mode, so they are measured on a
 * SYNTHETIC element carrying the real classes, mounted inside the real chat
 * view. That is stated rather than hidden: it measures the stylesheet's answer
 * for those classes on that backdrop, not the transport-failure flow itself.
 *
 * WHAT THIS GUARD CANNOT SEE — written down because a guard that reads as
 * exhaustive and is not is worse than no guard:
 *   - `/about`. A static page outside the SPA; this suite never navigates to
 *     it. `public/about.css` is covered by the static `--faint` half instead.
 *   - SIGNED-IN SURFACES. The demo config runs with `VITE_API_LIVE: "false"`,
 *     so `AuthModal`, `HistoryDrawer`, `AccountMenu` and
 *     `ConnectedAccountsDrawer` never mount. `--color-error` is used inline at
 *     13-14px in two of them (`AuthModal.tsx:480`, `HistoryDrawer.tsx:136`);
 *     those ratios are COMPUTED in `tokens.css`'s comment, not rendered here.
 *   - States demo mode cannot reach: the pending bubble, the live-answer
 *     suggestion list, real transport failures.
 *   - THE ELEVEN BLOCKED RUNS, enumerated and justified in `ALLOWED_BLOCKERS`
 *     below. They are reported, not skipped — a NEW unmodellable region fails
 *     the blocked-list test by name.
 *   - Anything a floor cannot express: text over a photo, a moving gradient,
 *     colour used as the only carrier of meaning (SC 1.4.1).
 *
 * ONE LINE PER TEST names the change that turns it red.
 */
import { test, expect } from "./fixtures";
import { ensureTheme, enterChat, gotoApp, waitStreamDone, type ThemeName } from "./helpers";
import { installKit, wcagFloor, type FloorSweep } from "./contrast-kit";

const THEMES: ThemeName[] = ["light", "dark"];

/** iPhone 14 metrics — under both of `landing.css`'s breakpoints (900px, 600px). */
const PHONE = { width: 390, height: 844 };

/**
 * The ancestor shapes whose paint the flat-`background-color` compositor
 * genuinely cannot model, each with the reason it is allowed to stay
 * unmeasured. A blocked run whose blocker is NOT on this list fails the test.
 *
 * Matching is on the blocker STRING's leading `element property:` part, not on
 * the blocked element's text, so a copy edit does not move the guard.
 */
const ALLOWED_BLOCKERS: { prefix: string; why: string }[] = [
  {
    prefix: "header.header backdrop-filter:",
    // `background: color-mix(in srgb, var(--bg) 82%, transparent)` over a
    // 12px blur of whatever has scrolled under it. The backdrop genuinely is
    // not a flat colour, and it changes as the page scrolls.
    why: "the sticky header is a translucent blur over scrolling content",
  },
  {
    prefix: "span.highlight background-image:",
    why: "the yellow underline band is a gradient; fidelity.spec.ts D2.5 checks it from real PIXELS",
  },
  {
    prefix: "span.highlight-phrase background-image:",
    why: "same gradient band, small variant; same pixel check covers it",
  },
  {
    prefix: "button.cta.cta-outlined opacity:",
    // `landing.css`: "Enterprise 'Contact sales' is a true no-op: a disabled
    // button is inert to both mouse and keyboard, and removed from the tab
    // order." WCAG 1.4.3 exempts inactive user-interface components.
    why: "a :disabled control, which SC 1.4.3 explicitly exempts",
  },
];

/** Selectors #403 retuned, with the theme(s) in which they were below AA. */
type Probe = {
  selector: string;
  pseudo?: string;
  why: string;
  /** Mount it first — demo mode does not reach this state on its own. */
  synthetic?: boolean;
};

const RETUNED: Probe[] = [
  { selector: ".refusal-badge", why: "--color-warning on --hl-soft: 3.20 light / 3.24 dark" },
  { selector: ".compare-footer.good", why: "--color-success on --surface: 3.60 light" },
  { selector: ".compare-footer.bad", why: "--color-error-strong on --surface: 3.22 dark" },
  { selector: ".source-badge.zero", why: "--color-error-strong on --surface-2: 2.92 dark" },
  { selector: ".check-icon", why: "--bg on the --color-success disc: 3.42 light" },
  { selector: ".hero-nudge", why: "--color-warning on --bg: 3.68 light" },
  {
    selector: "#hero-input",
    pseudo: "::placeholder",
    why: "unstyled, so Chromium's rgb(117, 117, 117) in BOTH themes: 3.61 dark",
  },
  { selector: ".notice-rate-limit", synthetic: true, why: "hardcoded amber: 4.31 light / 3.17 dark" },
  { selector: ".notice-error", synthetic: true, why: "hardcoded red: 2.67 dark" },
];

/**
 * Put the landing page into the state that renders every probe except the
 * nudge: the lazy below-the-fold strip mounted and the REFUSAL demo answer
 * chosen.
 */
async function openEveryProbedState(page: import("@playwright/test").Page) {
  await page.locator("#pricing").scrollIntoViewIfNeeded();
  await expect(page.locator("footer")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 0));

  // The refusal demo answer — `.refusal-badge` renders only for this one.
  await page.locator(".demo-question").nth(3).click();
  await waitStreamDone(page);
  await expect(page.locator(".demo-right .refusal-badge")).toBeVisible();
}

/**
 * Show the empty-Ask nudge and hold until its fade-up has actually landed.
 *
 * SEPARATE from `openEveryProbedState`, and always called immediately before
 * the measurement that needs it, because `useLandingState.ts` clears the nudge
 * on a hard 3000 ms timer. Opening it early and measuring after a few round
 * trips is a race the measurement loses silently — the element is simply gone
 * and the probe reports "matched zero elements" on a slow machine only.
 *
 * The opacity poll is not politeness either. `.hero-nudge` is
 * `animation: cv-fadeup 0.25s ease both`, so at the instant it appears its
 * computed `opacity` is 0 — the compositor then correctly REFUSES to score it
 * and the run lands in `blocked` rather than being measured. Measured: sampling
 * immediately reports `opacity:0`, the sweep sees nothing, and a genuine
 * 3.68:1 failure reads as a pass.
 */
async function showNudge(page: import("@playwright/test").Page) {
  await page.locator(".ask-button").click();
  await expect(page.locator(".hero-nudge")).toBeVisible();
  await expect
    .poll(() => page.locator(".hero-nudge").evaluate((el) => getComputedStyle(el).opacity), {
      timeout: 2000,
    })
    .toBe("1");
}

/**
 * Mount the two transport-failure badges where a real one would render.
 *
 * Demo mode never produces a 429 or a 5xx, so `ChatView`'s `errorKind` branches
 * are unreachable here and the only honest alternative to this is not measuring
 * them at all — which is how they shipped at 2.67:1. The badge is appended to a
 * REAL `.message.bot-msg .content`, the exact parent
 * `ChatView.tsx:536`/`:539` give it, so the backdrop it composites over is the
 * one a reader would get. What this does NOT exercise is the transport-failure
 * flow itself; that belongs to the live suite.
 */
async function mountSyntheticNotices(page: import("@playwright/test").Page) {
  // A real answer first, so a real `.content` exists to host the badge.
  await page.locator(".chat-input").fill("What is Claude Code?");
  await page.keyboard.press("Enter");
  await waitStreamDone(page);
  await expect(page.locator(".message.bot-msg .content").first()).toBeVisible();

  const mounted = await page.evaluate(() => {
    const host = document.querySelector(".message.bot-msg .content");
    if (!host) return 0;
    for (const cls of ["notice-rate-limit", "notice-error"]) {
      const el = document.createElement("div");
      el.className = `notice-badge ${cls}`;
      el.textContent = "SYNTHETIC PROBE";
      host.appendChild(el);
    }
    return document.querySelectorAll(".notice-badge").length;
  });
  // The precondition, asserted rather than assumed: if the host selector went
  // away, nothing would be mounted and the probes below would report "matched
  // zero elements" — a failure whose cause reads as a renamed class rather than
  // as this helper.
  expect(mounted, "the synthetic notice badges were not mounted into a real answer bubble").toBe(2);
}

function reportFindings(label: string, sweep: FloorSweep): string[] {
  return sweep.findings.map(
    (f) =>
      `${label}: ${f.what} — ${f.ratio.toFixed(2)}:1 needs ${f.floor}:1 ` +
      `(${f.fontSize}px/${f.fontWeight}, ${f.colour} on ${f.backdrop})`,
  );
}

for (const theme of THEMES) {
  test.describe(`[${theme}] every text run clears its WCAG floor (#403)`, () => {
    test.beforeEach(async ({ page }) => {
      await installKit(page);
      await gotoApp(page);
      await ensureTheme(page, theme);
    });

    test("the landing page, in every state the demo can reach", async ({ page }, testInfo) => {
      // RED WHEN: any of the nine selectors in `RETUNED` goes back to its
      // pre-#403 colour — verified one at a time, each reverting to exactly the
      // before-ratio quoted in this file's header.
      await openEveryProbedState(page);
      await showNudge(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      await testInfo.attach(`floor-landing-${theme}`, {
        body: JSON.stringify(sweep, null, 2),
        contentType: "application/json",
      });
      // NON-VACUITY, derived rather than a constant threshold pulled from
      // nowhere: a sweep that walked nothing reports zero findings and is
      // indistinguishable from a clean page. `measured` counts runs that were
      // actually scored, so it also catches a compositor that started refusing
      // everything.
      expect(sweep.examined, "the sweep visited nothing").toBeGreaterThan(800);
      expect(sweep.measured, "the sweep scored nothing — it is not measuring the page").toBeGreaterThan(200);
      expect(reportFindings("landing", sweep)).toEqual([]);
    });

    test("the landing page at a PHONE viewport", async ({ page }) => {
      // The bypass #396 learned the hard way: a desktop-only suite cannot see
      // `@media (max-width: …)` rules at all, so a colour added inside one is
      // invisible to every gate while every phone visitor renders it.
      // RED WHEN: a below-floor `color` is added to either `@media (max-width:)`
      // block in `landing.css` — verified with `.composer-hint { color: var(--faint) }`
      // in the 600px block, which this test reports and the desktop sweep does not.
      await page.setViewportSize(PHONE);
      const active = await page.evaluate(() => ({
        under900: matchMedia("(max-width: 900px)").matches,
        under600: matchMedia("(max-width: 600px)").matches,
        heroDescription: getComputedStyle(document.querySelector(".hero-description")!).fontSize,
      }));
      expect(active.under900, "the 900px breakpoint is not active at this viewport").toBe(true);
      expect(active.under600, "the 600px breakpoint is not active at this viewport").toBe(true);
      // 19px is the desktop rule; the 600px block overrides it to 16px. Without
      // this, a viewport change that silently stopped applying would leave two
      // identical sweeps.
      expect(active.heroDescription, "the mobile override did not take effect").toBe("16px");

      await openEveryProbedState(page);
      await showNudge(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      expect(sweep.measured, "the phone sweep scored nothing").toBeGreaterThan(200);
      expect(reportFindings("phone", sweep)).toEqual([]);
    });

    test("the chat view, transport-failure badges included", async ({ page }) => {
      // RED WHEN: `.notice-rate-limit` or `.notice-error` goes back to its
      // hardcoded rgba() — verified, and the dark theme reports 3.17:1 and
      // 2.67:1 respectively.
      await enterChat(page);
      await mountSyntheticNotices(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      expect(sweep.measured, "the chat sweep scored nothing").toBeGreaterThan(10);
      expect(reportFindings("chat", sweep)).toEqual([]);
    });

    test("every selector #403 retuned, measured by name with its floor", async ({ page }, testInfo) => {
      // The whole-page sweeps above report a CLEAN page many ways — including
      // by never reaching the elements in question. This names each one, fails
      // when it matches nothing, and records the after-ratio.
      // RED WHEN: any listed class is renamed (the zero-match assertion fires by
      // name), or any listed run drops below its floor.
      await openEveryProbedState(page);
      const report: Record<string, string> = {};
      const missing: string[] = [];
      const tooLow: string[] = [];
      const blocked: string[] = [];

      const measureAll = async (probes: Probe[]) => {
        for (const { selector, pseudo, why } of probes) {
          const n = await page.evaluate((s) => window.__cv396.count(s), selector);
          if (n === 0) {
            missing.push(`${selector} (${why})`);
            continue;
          }
          for (let i = 0; i < n; i++) {
            const run = await page.evaluate(
              ([s, idx, p]) =>
                window.__cv396.measure(s as string, idx as number, (p as string) || undefined),
              [selector, i, pseudo ?? ""] as const,
            );
            if (!run) {
              missing.push(`${selector}[${i}] could not be measured (${why})`);
              continue;
            }
            if (run.blockers.length) {
              blocked.push(`${selector}[${i}] sits under ${run.blockers.join("; ")}`);
              continue;
            }
            const key = `${selector}${pseudo ?? ""}[${i}]`;
            report[key] = `${run.ratio.toFixed(2)}:1 (floor ${run.floor}) ${run.colour} on ${run.backdrop}`;
            if (run.ratio < run.floor) {
              tooLow.push(`${key} ${run.ratio.toFixed(2)}:1 needs ${run.floor}:1 — ${why}`);
            }
          }
        }
      };

      await measureAll(RETUNED.filter((p) => !p.synthetic && p.selector !== ".hero-nudge"));
      // The nudge is opened LAST on this screen and measured immediately: it is
      // cleared on a hard 3 s timer, so measuring it after the loop above would
      // be a race this test loses only on a slow machine.
      await showNudge(page);
      await measureAll(RETUNED.filter((p) => p.selector === ".hero-nudge"));
      await enterChat(page);
      await mountSyntheticNotices(page);
      await measureAll(RETUNED.filter((p) => p.synthetic));

      await testInfo.attach(`retuned-${theme}`, {
        body: JSON.stringify(report, null, 2),
        contentType: "application/json",
      });
      expect(missing, "these matched zero elements, so they measured nothing").toEqual([]);
      expect(blocked, "the composite cannot be trusted over these ancestors").toEqual([]);
      expect(tooLow, "below their WCAG floor").toEqual([]);
      // One measurement per probe, at least — the loop must have run.
      expect(Object.keys(report).length).toBeGreaterThanOrEqual(RETUNED.length);
    });

    test("the runs it REFUSES to measure are only the four known shapes", async ({ page }) => {
      // A sweep can also be clean because it declared everything unmeasurable.
      // This pins the refusals to an enumerated list with a written reason each,
      // so a new gradient, filter or translucent ancestor over real text is a
      // named failure rather than a silent hole.
      // RED WHEN: a new unmodellable ancestor is introduced over text, or
      // `blockersOf`'s `stopAtOpaque` early exit is removed (which reports every
      // element under any decorated ancestor and the list explodes).
      await openEveryProbedState(page);
      await showNudge(page);
      const sweep = await page.evaluate(() => window.__cv396.floorSweep());

      // Non-vacuity: the four shapes below EXIST on this page, so an empty
      // `blocked` list means the blocker detection died, not that the page got
      // simpler.
      expect(sweep.blocked.length, "nothing was reported as unmeasurable — the blocker pass is dead")
        .toBeGreaterThan(0);

      const unexplained = sweep.blocked.filter(
        (entry) => !ALLOWED_BLOCKERS.some(({ prefix }) => entry.includes(prefix)),
      );
      expect(
        unexplained,
        "these text runs sit under something this compositor cannot model, and no reason is " +
          "recorded for them in ALLOWED_BLOCKERS",
      ).toEqual([]);

      // And every allowed shape is still PRESENT. Without this the allow-list
      // rots into a list of things that stopped existing, and the day one comes
      // back it is waved through by a rule nobody re-read.
      for (const { prefix, why } of ALLOWED_BLOCKERS) {
        expect(
          sweep.blocked.some((entry) => entry.includes(prefix)),
          `no run is blocked by "${prefix}" any more (${why}) — drop it from ALLOWED_BLOCKERS ` +
            "so the list keeps meaning what it says",
        ).toBe(true);
      }
    });

    test("the sweep can still SEE a run below its floor", async ({ page }) => {
      // The detector-still-works partner. Four green "no findings" results above
      // are indistinguishable from a sweep that returns [] unconditionally.
      //
      // TWO elements are planted, the COMPLIANT one first, and both are appended
      // at the very end of <body>. That shape is deliberate: a `continue` turned
      // into a `break` anywhere in the sweep's loops stops the walk early, and
      // an offender planted at the end is then never reached. Verified by making
      // that mutation.
      // RED WHEN: `floorSweep` stops comparing `ratio` against `floor`, or any
      // `continue` in its loops becomes a `break`.
      const before = await page.evaluate(() => window.__cv396.floorSweep());
      expect(reportFindings("pre-plant", before)).toEqual([]);

      const planted = await page.evaluate(() => {
        const mk = (cls: string, colour: string) => {
          const el = document.createElement("p");
          el.className = cls;
          el.textContent = "planted";
          el.style.color = colour;
          el.style.background = "#ffffff";
          el.style.fontSize = "12px";
          document.body.appendChild(el);
          return getComputedStyle(el).color;
        };
        return {
          // #595959 on white is 7.00:1 — comfortably over the floor.
          compliant: mk("cv403-compliant", "#595959"),
          // #949494 on white is 2.85:1 — under 4.5:1 at 12px.
          offender: mk("cv403-planted-offender", "#949494"),
        };
      });
      // Preconditions: both plants really took the colours they were given, so
      // "reported" and "not reported" mean what they say.
      expect(planted.compliant).toBe("rgb(89, 89, 89)");
      expect(planted.offender).toBe("rgb(148, 148, 148)");

      const after = await page.evaluate(() => window.__cv396.floorSweep());
      const named = after.findings.map((f) => f.what).join(" | ");
      expect(named, "the planted low-contrast run was not reported").toContain(
        "cv403-planted-offender",
      );
      expect(named, "the COMPLIANT plant was reported as a failure").not.toContain(
        "cv403-compliant",
      );
      expect(after.findings.length, "exactly one plant should be reported").toBe(1);

      await page.evaluate(() => {
        document.querySelector(".cv403-planted-offender")?.remove();
        document.querySelector(".cv403-compliant")?.remove();
      });
      expect(
        reportFindings("post-plant", await page.evaluate(() => window.__cv396.floorSweep())),
      ).toEqual([]);
    });

    test("the sweep scores -webkit-text-fill-color, which OVERRIDES color on the glyphs", async ({
      page,
    }) => {
      // The bypass #396 measured and closed for its token ban, asserted here for
      // the floor. `getComputedStyle(el).color` keeps reporting the legible
      // value while the glyphs paint at the fill colour, so a `color`-only
      // sweep reads a comfortable ratio off text nobody can read.
      // RED WHEN: `score` stops preferring `webkitTextFillColor` over `color`.
      const readings = await page.evaluate(() => {
        const el = document.createElement("p");
        el.className = "cv403-fill-offender";
        el.textContent = "planted";
        el.style.background = "#ffffff";
        el.style.fontSize = "12px";
        // `color` stays LEGIBLE (7.00:1). Only the fill is illegible (2.85:1),
        // so nothing but the text-fill pass can find this element.
        el.style.color = "#595959";
        el.style.webkitTextFillColor = "#949494";
        document.body.appendChild(el);
        const s = getComputedStyle(el);
        return { color: s.color, fill: s.webkitTextFillColor };
      });
      // The precondition: if the browser had refused the property the plant
      // would be a no-op and the assertion below would pass for the wrong
      // reason.
      expect(readings.color, "the plant's `color` was not left legible").not.toBe(readings.fill);

      const run = await page.evaluate(() =>
        window.__cv396.measure(".cv403-fill-offender", 0),
      );
      expect(run, "the plant was not found").not.toBeNull();
      expect(run!.colour, "the fill colour is not what was scored").toContain("148, 148, 148");
      expect(run!.ratio).toBeLessThan(run!.floor);

      const sweep = await page.evaluate(() => window.__cv396.floorSweep());
      expect(sweep.findings.map((f) => f.what).join(" | ")).toContain("cv403-fill-offender");

      await page.evaluate(() => document.querySelector(".cv403-fill-offender")?.remove());
      expect(
        reportFindings("post-plant", await page.evaluate(() => window.__cv396.floorSweep())),
      ).toEqual([]);
    });

    test("a wrapper div is not scored for text its children paint", async ({ page }) => {
      // The shape that makes a whole-page floor sweep either useful or noise.
      // Scoring every element that merely CONTAINS text would report each run
      // once per ancestor, at whatever colour each ancestor inherited — so a
      // legible `<span>` inside an illegible-coloured `<div>` would be a
      // finding, and real failures would drown.
      // RED WHEN: `paintsText` stops requiring a non-whitespace DIRECT child
      // text node (e.g. it falls back to `el.textContent`).
      const seen = await page.evaluate(() => {
        const wrap = document.createElement("div");
        wrap.className = "cv403-wrapper";
        wrap.style.color = "#949494"; // 2.85:1 — below the floor
        wrap.style.background = "#ffffff";
        wrap.style.fontSize = "12px";
        const child = document.createElement("span");
        child.className = "cv403-child";
        child.style.color = "#595959"; // 7.00:1 — over it
        child.textContent = "legible";
        wrap.appendChild(child);
        document.body.appendChild(wrap);
        const out = window.__cv396.floorSweep().findings.map((f) => f.what);
        document.querySelector(".cv403-wrapper")?.remove();
        return out;
      });
      expect(
        seen.join(" | "),
        "the wrapper was scored for its child's text, at the wrapper's own colour",
      ).not.toContain("cv403-wrapper");

      // The partner: a wrapper that owns its OWN text node IS scored, so the
      // rule above is "no direct text", not "never score a div".
      const withOwnText = await page.evaluate(() => {
        const wrap = document.createElement("div");
        wrap.className = "cv403-wrapper-with-text";
        wrap.style.color = "#949494";
        wrap.style.background = "#ffffff";
        wrap.style.fontSize = "12px";
        wrap.appendChild(document.createTextNode("its own words"));
        const child = document.createElement("span");
        child.style.color = "#595959";
        child.textContent = "legible";
        wrap.appendChild(child);
        document.body.appendChild(wrap);
        const out = window.__cv396.floorSweep().findings.map((f) => f.what);
        document.querySelector(".cv403-wrapper-with-text")?.remove();
        return out;
      });
      expect(
        withOwnText.join(" | "),
        "a wrapper with its OWN illegible text node was not scored",
      ).toContain("cv403-wrapper-with-text");
    });

    test("the floor follows WCAG's large-text definition, at its boundaries", async () => {
      // `wcagFloor` is pure, so this needs no page — but it runs beside the
      // sweeps that consume it, where a reader will look for it.
      //
      // A guard that applied one flat 4.5:1 to everything would be stricter than
      // the criterion and would fail legitimate display type; one that applied
      // 3:1 to everything would wave through the 9px and 10px runs this change
      // exists to fix. The boundary is where both mistakes live.
      // RED WHEN: either threshold moves, or the weight condition is dropped
      // from the 18.667px branch.
      expect(wcagFloor(23.9, 400)).toBe(4.5);
      expect(wcagFloor(24, 400)).toBe(3);
      expect(wcagFloor(18.7, 400), "large only at >= 700 below 24px").toBe(4.5);
      expect(wcagFloor(18.7, 700)).toBe(3);
      expect(wcagFloor(18.5, 700), "18.5px is under 14pt even at weight 700").toBe(4.5);
      expect(wcagFloor(24, 700)).toBe(3);
      // The sizes this app actually ships at the failing end.
      expect(wcagFloor(9, 400)).toBe(4.5);
      expect(wcagFloor(10, 700)).toBe(4.5);
      expect(wcagFloor(11, 600)).toBe(4.5);
      expect(wcagFloor(12.5, 400)).toBe(4.5);
      expect(wcagFloor(16, 400)).toBe(4.5);
    });
  });
}
