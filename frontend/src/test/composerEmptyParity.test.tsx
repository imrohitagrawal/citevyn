/**
 * #445 — CiteVyn has TWO places to type a question, and they disagreed on
 * identical input.
 *
 * The landing hero focused the box, flashed an amber border and rendered a
 * warning. `submitChat` had `if (!t) return;` — a bare return, one line above
 * the in-flight refusal whose own comment records that a bare return produces
 * ZERO DOM mutations on both the Enter path and the click path. Someone fixed
 * the silent return below it and left this one.
 *
 * A unit test for the chat composer would have closed the INSTANCE. This file
 * closes the CLASS: it drives both composers through the same empty submit,
 * records what a reader actually receives from each, and requires the two
 * records to be equal AND to be the right record. It goes red if either
 * composer regresses ON WHAT IT RECORDS — including a future "fix" that
 * improves only one of them, which is exactly how the pair got here. An earlier
 * draft of this line said "if EITHER composer regresses", flat; the list of
 * what it does not record is below, and that wording claimed all of it.
 *
 * Modelled on `tokenThemeParity.test.ts`, including its two habits:
 *   - a PARTNER test proving the thing counted exists, because "the two records
 *     are equal" is vacuously true when both are empty, which is what a renamed
 *     selector or a screen that failed to open produces; and
 *   - a DETECTOR test proving the observation function can still tell the two
 *     states apart, because a green "no differences" is indistinguishable from
 *     an observer that returns the same constant whatever it is given.
 *
 * WHAT THIS CANNOT SEE, stated because a guard that reads as exhaustive and is
 * not is worse than no guard:
 *   - whether the nudge is legible, or whether it reaches a real browser at
 *     all. Colour is `frontend/tests/contrast-floor.spec.ts`, which measures
 *     `.hero-nudge` by name in its `RETUNED` list and `.composer-nudge` through
 *     the whole-page chat sweep (this change raises the nudge before that
 *     sweep runs, which is all it took — a shared stylesheet rule is NOT on its
 *     own a shared contrast reading, because contrast is foreground AND
 *     background and the two agree only while both composite onto `--bg`).
 *     Behaviour in a browser is `frontend/tests/behavior.spec.ts`, which clicks
 *     the arrow on an empty box and checks focus — the one thing jsdom cannot
 *     honestly prove. This file asserts nothing about either.
 *   - whether a screen reader really speaks the region. jsdom has no
 *     accessibility tree; what is asserted here is the DOM contract that makes
 *     the announcement possible — a `role="status"` node present BEFORE its text
 *     changes, carrying the sentence.
 *   - A THIRD COMPOSER. `COMPOSERS` below is a hand-kept list, which is this
 *     guard's one weakness, written down rather than left implied. The registry
 *     assertion at the bottom catches the list being TRUNCATED, not a new
 *     composer being added elsewhere and never listed.
 *
 * ONE LINE PER TEST names the change that turns it red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { LandingPage } from "../components/LandingPage";
import { isLiveMode, createSession, askQuestion } from "../lib/api";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import postcss, { Rule } from "postcss";
import {
  EMPTY_SUBMIT_NUDGE,
  EMPTY_SUBMIT_NUDGE_GLYPH,
  EMPTY_SUBMIT_NUDGE_MS,
} from "../lib/composerNudge";

vi.mock("../lib/api", () => ({
  isLiveMode: vi.fn(() => false),
  createSession: vi.fn(),
  askQuestion: vi.fn(),
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

// jsdom implements no `scrollIntoView`, and the duplicate-question flash calls
// it. Stubbed the way `ChatView.test.tsx` already does, and put BACK afterwards
// rather than left on the prototype for whatever runs next in this worker.
const realScrollIntoView = Element.prototype.scrollIntoView;

beforeEach(() => {
  vi.useFakeTimers();
  Element.prototype.scrollIntoView = vi.fn();
  vi.mocked(isLiveMode).mockReturnValue(false);
  vi.mocked(createSession).mockReset();
  vi.mocked(askQuestion).mockReset();
});

afterEach(() => {
  cleanup();
  Element.prototype.scrollIntoView = realScrollIntoView;
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// The two composers
// ---------------------------------------------------------------------------

interface Composer {
  label: string;
  /** Open the screen this composer lives on. The page boots on the landing
      screen, so the hero needs nothing and the chat needs one click. */
  open: (root: HTMLElement) => void;
  /** The region that owns this composer. Every query below is scoped to it, so
      neither composer can pass on the other's DOM. */
  scope: string;
  input: string;
  /** This composer's submit control. Also the focus THIEF: focus is parked here
      before the click, so "focus moved to the input" measures the nudge and not
      the chat screen's mount-time autofocus. */
  submitButton: string;
  /** The visible warning. Two class names, one stylesheet rule. */
  nudge: string;
}

/**
 * Did a question reach the transcript? ONE probe for both composers.
 *
 * The hero's used to be "did we navigate to the chat screen", and a review
 * showed that is not the same question: `.chat-screen` is also what the header
 * CTA produces, which is how `open` below reaches the chat composer without
 * asking anything. Point `askHero` at `enterChat(null)` instead of
 * `enterChat(q)` and the old probe stayed green while the reader's question was
 * silently dropped. A user bubble is the thing that actually means "sent", on
 * both screens, so both use it.
 */
const sentAnything = (root: HTMLElement) => root.querySelector(".message.user-msg") !== null;

/**
 * EVERY composer in this app. Both entries are exercised by identical code
 * below; nothing here is composer-specific except the selectors and the "did it
 * send" probe, which is the point.
 */
const COMPOSERS: Composer[] = [
  {
    label: "hero",
    open: () => {},
    scope: "section.hero",
    input: "#hero-input",
    submitButton: ".ask-button",
    nudge: ".hero-nudge",
  },
  {
    label: "chat",
    // `.cta-button` is the header's "Ask" — `enterChat(null)`, which opens the
    // chat screen WITHOUT asking anything. Entering by typing a hero question
    // would make `sent` true before the test began.
    open: (root) => {
      const cta = root.querySelector(".cta-button") as HTMLElement | null;
      expect(cta, "the header CTA that opens the chat screen was not found").not.toBeNull();
      act(() => {
        fireEvent.click(cta!);
      });
    },
    scope: ".composer",
    input: ".chat-input",
    submitButton: ".send-button",
    nudge: ".composer-nudge",
  },
];

/** Registered by each generated `describe`. See the last test in this file. */
const EXERCISED: string[] = [];

/**
 * The comparator, at module scope so the parity test and its detector below use
 * the SAME code. An earlier draft had the detector build a private copy, which
 * meant its "RED WHEN the comparison is weakened to a no-op" was false — the
 * comparison it guarded was not the one the parity test ran, and stubbing the
 * real one left the detector green. A detector for a different function is not
 * a detector.
 */
function diffRecords(a: Observation, b: Observation): (keyof Observation)[] {
  return (Object.keys(a) as (keyof Observation)[]).filter((k) => a[k] !== b[k]);
}

/** What a reader receives. Every field is compared across the two composers. */
interface Observation {
  focusIsOwnInput: boolean;
  visibleNudgeCount: number;
  visibleNudgeText: string;
  /** The persistent `role="status"` region, which must EXIST whether or not it
      is saying anything — a live region added together with its text is the
      classic way to make an announcement not fire, and is why the hero's
      "correct" nudge was not announced either before #445.

      LOAD-BEARING FOR THE HERO ONLY, said plainly so nobody reads the chat half
      as protection it is not giving: the chat's region is pre-existing and
      roughly twenty assertions in `ChatView.test.tsx` already call
      `getByRole("status")`, which throws on more than one. The hero's region is
      new here and nothing else asserts it. */
  statusRegionCount: number;
  announced: string;
  /** The visible warning duplicates the announced sentence verbatim, so it is
      `aria-hidden` on both screens; without it a reader meets the sentence
      twice. Recorded because it is a property of the pair, not of one. */
  visibleNudgeHidden: string;
  sent: boolean;
}

function observe(root: HTMLElement, c: Composer): Observation {
  const scope = root.querySelector(c.scope);
  // Not an optional chain that quietly reports zeroes: a scope that failed to
  // open is the state every "0" and "" below is vacuously happy with.
  expect(scope, `${c.label}: its scope (${c.scope}) is not on screen`).not.toBeNull();
  const nudges = scope!.querySelectorAll(c.nudge);
  const regions = scope!.querySelectorAll('[role="status"]');
  return {
    focusIsOwnInput: document.activeElement === scope!.querySelector(c.input),
    visibleNudgeCount: nudges.length,
    visibleNudgeText: Array.from(nudges).map((n) => n.textContent ?? "").join("|"),
    statusRegionCount: regions.length,
    announced: Array.from(regions).map((n) => n.textContent ?? "").join("|"),
    visibleNudgeHidden: Array.from(nudges)
      .map((n) => n.getAttribute("aria-hidden") ?? "<absent>")
      .join("|"),
    sent: sentAnything(root),
  };
}

/** Park focus on the submit button, then click it. */
function clickSubmitEmpty(root: HTMLElement, c: Composer) {
  const scope = root.querySelector(c.scope)!;
  const button = scope.querySelector(c.submitButton) as HTMLElement | null;
  expect(button, `${c.label}: ${c.submitButton} was not found`).not.toBeNull();
  act(() => {
    button!.focus();
  });
  // The precondition that makes `focusIsOwnInput` mean anything. The chat
  // screen autofocuses its composer on mount, so without this the focus
  // assertion below would be green with the nudge deleted entirely.
  expect(
    document.activeElement,
    `${c.label}: focus should start on the button, not the input`,
  ).toBe(button);
  act(() => {
    fireEvent.click(button!);
  });
}

// ---------------------------------------------------------------------------
// Per-composer behaviour
// ---------------------------------------------------------------------------

for (const c of COMPOSERS) {
  describe(`${c.label} composer: an empty submit is seen and heard`, () => {
    EXERCISED.push(c.label);

    it("focuses the box, shows the warning, announces it, and sends nothing", () => {
      // RED WHEN: this composer's empty branch stops focusing, stops setting its
      // nudge flag, stops rendering the visible warning, drops the persistent
      // `role="status"` region, or starts sending an empty question. Reverting
      // `submitChat`'s branch to the bare `return` it shipped with turns the
      // chat half of this red on four fields at once.
      const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
      c.open(container);
      clickSubmitEmpty(container, c);

      const o = observe(container, c);
      expect(o.focusIsOwnInput, `${c.label}: focus did not move to the box`).toBe(true);
      expect(o.visibleNudgeCount, `${c.label}: no visible nudge`).toBe(1);
      expect(o.visibleNudgeText).toBe(`${EMPTY_SUBMIT_NUDGE_GLYPH} ${EMPTY_SUBMIT_NUDGE}`);
      expect(o.statusRegionCount, `${c.label}: no persistent status region`).toBe(1);
      expect(o.announced, `${c.label}: the region said nothing`).toBe(EMPTY_SUBMIT_NUDGE);
      expect(o.visibleNudgeHidden, `${c.label}: the warning is announced twice`).toBe("true");
      expect(o.sent, `${c.label}: an EMPTY question was sent`).toBe(false);
    });

    it("clears the warning after the window, and re-arming RESTARTS it", () => {
      // Two claims in one test because the second is only meaningful against the
      // first: without "it clears at all", "it is still up at 3.5s" is satisfied
      // by a nudge that never goes away.
      // RED WHEN: the timeout stops being armed (never clears), OR the
      // `timers.current[...]?.stop()` before the re-arm is removed. Traced on
      // the fake clock rather than described from memory, because an earlier
      // draft of this line got the number wrong: the three clicks land at
      // t=0 / t=3001 / t=5001 and the final read is at t=6501, so a handle left
      // armed at 3001 fires at 6001 — 1.0s into a window the reader had already
      // superseded, and 500ms before this test looks.
      const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
      c.open(container);
      const scope = () => container.querySelector(c.scope)!;
      const nudgeCount = () => scope().querySelectorAll(c.nudge).length;

      clickSubmitEmpty(container, c);
      expect(nudgeCount(), `${c.label}: nothing to clear`).toBe(1);

      act(() => {
        vi.advanceTimersByTime(EMPTY_SUBMIT_NUDGE_MS - 1);
      });
      expect(nudgeCount(), `${c.label}: cleared EARLY`).toBe(1);
      act(() => {
        vi.advanceTimersByTime(2);
      });
      expect(nudgeCount(), `${c.label}: never cleared`).toBe(0);

      // Re-arm: a second empty submit two thirds of the way through the window.
      clickSubmitEmpty(container, c);
      expect(nudgeCount()).toBe(1);
      act(() => {
        vi.advanceTimersByTime(2000);
      });
      clickSubmitEmpty(container, c);
      act(() => {
        // 1500ms past the SECOND submit — and 3500ms past the first, so a stale
        // first handle has fired by now if it was left running.
        vi.advanceTimersByTime(1500);
      });
      expect(
        nudgeCount(),
        `${c.label}: a superseded timer cleared the nudge 1.5s into its window`,
      ).toBe(1);
    });

    it("a REAL question is sent, with no warning — the detector still works", () => {
      // The partner that makes every "no nudge / not sent" reading above mean
      // something. Three green empty-submit results are indistinguishable from
      // an `observe` that returns the same record whatever it is given, and from
      // a composer that is simply broken and sends nothing ever.
      // RED WHEN: `observe` stops reading the DOM, this composer's selectors go
      // stale, or a real question stops reaching the screen.
      const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
      c.open(container);
      const input = container.querySelector(`${c.scope} ${c.input}`) as HTMLInputElement | null;
      expect(input, `${c.label}: ${c.input} was not found`).not.toBeNull();
      act(() => {
        fireEvent.change(input!, { target: { value: "What is Claude Code?" } });
        fireEvent.keyDown(input!, { key: "Enter" });
      });
      act(() => {
        vi.advanceTimersByTime(400);
      });

      expect(sentAnything(container), `${c.label}: a real question was NOT sent`).toBe(true);
      // The nudge is scoped to the composer, and the hero's scope is gone once
      // it navigates — so read the whole tree, which cannot report a false
      // absence just because a container unmounted. Both class names, not
      // `c.nudge` and both (which expanded to the hero's twice, harmlessly but
      // pointlessly): a real question must raise NEITHER composer's warning.
      expect(
        container.querySelectorAll(".hero-nudge, .composer-nudge").length,
        `${c.label}: a real question raised the empty-submit warning`,
      ).toBe(0);
      expect(container.textContent).not.toContain(EMPTY_SUBMIT_NUDGE);
    });
  });
}

// ---------------------------------------------------------------------------
// Parity
// ---------------------------------------------------------------------------

describe("the two composers behave IDENTICALLY on an empty submit", () => {
  /** One fresh page per composer; the records must come out equal. */
  function recordFor(c: Composer): Observation {
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    c.open(container);
    clickSubmitEmpty(container, c);
    const o = observe(container, c);
    cleanup();
    return o;
  }

  it("finds BOTH composers, each with a real input", () => {
    // The partner for the equality below, which is vacuously true when both
    // records are empty — exactly what a renamed selector or a screen that
    // failed to open produces. The floor is "these are real, distinct DOM
    // nodes", not a tuned number.
    // RED WHEN: a composer is dropped from `COMPOSERS`, or either screen stops
    // rendering its input.
    expect(COMPOSERS.map((c) => c.label)).toEqual(["hero", "chat"]);
    const seen: string[] = [];
    for (const c of COMPOSERS) {
      const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
      c.open(container);
      const input = container.querySelector(`${c.scope} ${c.input}`);
      expect(input, `${c.label}: ${c.input} is not on screen`).not.toBeNull();
      expect(input!.tagName).toBe("INPUT");
      seen.push(c.label);
      cleanup();
    }
    expect(seen, "a composer was skipped before it was ever rendered").toEqual(["hero", "chat"]);
  });

  it("produces the SAME record: focus, warning, announcement, and nothing sent", () => {
    // THE GUARD. Not "the chat composer nudges" — that is the instance. This is
    // the class: whatever one composer does on an empty submit, the other does.
    // RED WHEN: either composer gains or loses focus-move, the visible warning,
    // the persistent status region, the announced sentence, or the no-send —
    // including a change that "improves" only one of them, which is how the two
    // drifted apart in the first place.
    const [hero, chat] = COMPOSERS.map(recordFor);

    // Through `diffRecords`, the same function the detector below plants
    // differences into — so that detector is guarding THIS comparison and not a
    // private copy of it. `toEqual` on the whole record follows, so a field
    // added to `Observation` later is compared without anyone remembering to
    // add a line here.
    expect(diffRecords(hero, chat), "the two composers disagree on these").toEqual([]);
    expect(chat).toEqual(hero);

    // …and the shared record is the RIGHT one. Without this the test is happy
    // with two composers that are identically broken — both silent, both
    // sending nothing, perfectly in parity and useless.
    expect(hero).toEqual({
      focusIsOwnInput: true,
      visibleNudgeCount: 1,
      visibleNudgeText: `${EMPTY_SUBMIT_NUDGE_GLYPH} ${EMPTY_SUBMIT_NUDGE}`,
      statusRegionCount: 1,
      announced: EMPTY_SUBMIT_NUDGE,
      visibleNudgeHidden: "true",
      sent: false,
    });
  });

  it("the shared copy is the copy, not whatever the constants happen to say", () => {
    // WRITTEN BECAUSE TWO MUTANTS SURVIVED the first draft, both the same shape
    // as the one that already forced the `_MS` pin below: every string
    // expectation in this file is built FROM the constants the product imports,
    // so product and assertion move together and nothing notices. Rewriting
    // `EMPTY_SUBMIT_NUDGE` to any other non-empty sentence was green, and so was
    // emptying `EMPTY_SUBMIT_NUDGE_GLYPH` — the visible text became
    // " Type a question…" on both sides and the expectation became the same
    // string.
    //
    // Pinned here rather than inline so the parity assertions stay relative
    // (they are about the two agreeing) and exactly one place is absolute.
    // RED WHEN: the shared sentence or the glyph is edited without this line.
    expect(EMPTY_SUBMIT_NUDGE).toBe("Type a question first. Nothing was sent.");
    expect(EMPTY_SUBMIT_NUDGE_GLYPH).toBe("⚠");
    // The two independent claims the sentence has to keep. `frontend/tests/
    // behavior.spec.ts` asserts the first in a real browser; the second is the
    // NVDA punctuation rationale `ChatView.tsx` records beside `REFUSED_TEXT`,
    // which this string joins in the same live region.
    expect(EMPTY_SUBMIT_NUDGE).toContain("Type a question first");
    expect(EMPTY_SUBMIT_NUDGE, "an em dash in an sr-only string").not.toContain("—");
  });

  it("the comparator can still SEE a divergence", () => {
    // The detector-still-works partner, in the shape `tokenThemeParity.test.ts`
    // uses: a green equality is indistinguishable from a comparison that is a
    // no-op. This plants a difference in a COPY of a real record and requires it
    // to be reported.
    // RED WHEN: `diffRecords` — the comparator the parity test above actually
    // runs — stops reporting a difference, or `Observation` is reduced to
    // fields that cannot differ.
    const real = recordFor(COMPOSERS[0]);

    // The plants are DERIVED from the real record rather than written as
    // literals: a literal that stopped matching would leave the copy identical,
    // and "not reported" would then be correct behaviour reported as a pass.
    // Each precondition asserts the field HAS something to mangle first.
    expect(real.announced, "the record carried no announcement to mangle").not.toBe("");
    expect(real.visibleNudgeCount, "no nudge to remove").toBeGreaterThan(0);
    expect(real.sent, "the record already reads as sent").toBe(false);
    expect(diffRecords(real, { ...real, announced: "" })).toEqual(["announced"]);
    expect(diffRecords(real, { ...real, visibleNudgeCount: 0 })).toEqual(["visibleNudgeCount"]);
    expect(diffRecords(real, { ...real, sent: true })).toEqual(["sent"]);
  });
});

describe("the nudge window is a real number, not whatever the constant says", () => {
  it("is 3000ms, the value the browser specs wait out", () => {
    // WRITTEN BECAUSE A MUTANT SURVIVED. Retuning `EMPTY_SUBMIT_NUDGE_MS` to
    // 100000 left all ten tests above green: every timing assertion in this
    // file is expressed in terms of the constant itself, so the product and the
    // assertion move together and the file notices nothing. That is correct for
    // the RELATIVE claims it makes ("cleared after the window", "a superseded
    // timer did not clear it early") and blind to the absolute one.
    //
    // The number is not free. `frontend/tests/landing.spec.ts` waits a literal
    // 3300ms for the hero nudge to go, and `frontend/tests/behavior.spec.ts`
    // measures its lifetime against a literal (2500, 5000) band — both in a
    // real browser. This pin is the unit suite's cheap copy of a check that
    // otherwise only exists behind Playwright.
    // RED WHEN: the shared nudge window is retuned without those two specs.
    expect(EMPTY_SUBMIT_NUDGE_MS).toBe(3000);
  });
});

// ---------------------------------------------------------------------------
// The nudge must not eat anything else the region has to say
// ---------------------------------------------------------------------------

describe("the empty-submit nudge does not swallow an in-flight refusal", () => {
  /** Live mode, one question that never resolves, so the window stays open. */
  function renderWithOneQuestionInFlight() {
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(createSession).mockResolvedValue({
      request_id: "r",
      session_id: "s",
      expires_at: "2026-07-11T12:00:00Z",
    });
    vi.mocked(askQuestion).mockImplementation(() => new Promise(() => {}));
    return render(<LandingPage theme="light" onThemeChange={() => {}} />);
  }

  const regionText = (root: HTMLElement) =>
    root.querySelector('.composer [role="status"]')?.textContent ?? "";

  const typeAndEnter = (root: HTMLElement, selector: string, value: string) => {
    const input = root.querySelector(selector) as HTMLInputElement;
    act(() => {
      fireEvent.change(input, { target: { value } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
  };

  it("announces the refusal for a question typed INSIDE the nudge window", async () => {
    // FOUND BY REVIEW, not by the suite — and it is a regression this change
    // introduced, not a pre-existing gap. Both signals go through ONE
    // `role="status"` region, so whichever renders hides the other. Ordering the
    // nudge first (which a stale refusal otherwise swallows) opened the reverse:
    // a REAL question refused inside the 3s window announced nothing, and then
    // `SET_PENDING(0)` cleared `refusedInFlight` before the nudge expired, so it
    // was destroyed having never been spoken. Three user actions, no unusual
    // timing. The fix is in the reducer: `REFUSED_SUBMIT` clears `chatNudge`.
    // RED WHEN: `REFUSED_SUBMIT` stops clearing `chatNudge` — the region then
    // still reads the nudge sentence when the refusal lands.
    const { container } = renderWithOneQuestionInFlight();
    typeAndEnter(container, "#hero-input", "First question");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    // Partners: we are on the chat screen, a request really is open, and the
    // composer is empty — so what follows is the sequence and not a setup that
    // never got started.
    expect(container.querySelector(".chat-screen")).not.toBeNull();
    expect(container.querySelector(".pending-bubble")).not.toBeNull();
    expect((container.querySelector(".chat-input") as HTMLInputElement).value).toBe("");

    // Empty Enter -> the nudge takes the region.
    act(() => {
      fireEvent.keyDown(container.querySelector(".chat-input")!, { key: "Enter" });
    });
    expect(regionText(container), "the nudge never reached the region").toBe(EMPTY_SUBMIT_NUDGE);
    expect(container.querySelectorAll(".composer-nudge").length).toBe(1);

    // Half a second later, a REAL question, refused because one is in flight.
    act(() => {
      vi.advanceTimersByTime(500);
    });
    typeAndEnter(container, ".chat-input", "Second question");

    const said = regionText(container);
    expect(said, "the nudge swallowed the refusal").not.toBe(EMPTY_SUBMIT_NUDGE);
    expect(said, "the refusal was not announced").toContain("Not sent.");
    // The stale warning goes with it: "Nothing was sent" is still true of that
    // empty Enter, but it is no longer what just happened, and the box is no
    // longer empty.
    expect(
      container.querySelectorAll(".composer-nudge").length,
      "a stale empty-box warning outlived the question that replaced it",
    ).toBe(0);
    // …and the refusal's own promise is kept, which is what makes it the right
    // sentence to be showing.
    expect((container.querySelector(".chat-input") as HTMLInputElement).value).toBe(
      "Second question",
    );
  });

  it("does not raise a nudge that nobody asked for after a screen round-trip", () => {
    // Also from review. The flag outlived its screen: empty Enter in chat, then
    // "Back to landing" and the header CTA inside the 3s window, and a freshly
    // opened chat announced "Type a question first" about a submit the reader
    // never made. `heroNudge` leaked the same way and had done before this
    // change; `SET_SCREEN` now clears both.
    // RED WHEN: `SET_SCREEN` stops clearing the nudges.
    vi.mocked(isLiveMode).mockReturnValue(false);
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    const cta = () => container.querySelector(".cta-button") as HTMLElement;
    act(() => {
      fireEvent.click(cta());
    });
    act(() => {
      fireEvent.keyDown(container.querySelector(".chat-input")!, { key: "Enter" });
    });
    // Partner: the nudge really is up, so its absence below is a clear and not
    // a submit that never registered.
    expect(container.querySelectorAll(".composer-nudge").length).toBe(1);

    act(() => {
      fireEvent.click(container.querySelector(".back-button") as HTMLElement);
      vi.advanceTimersByTime(500);
    });
    act(() => {
      fireEvent.click(cta());
      vi.advanceTimersByTime(200);
    });

    expect(
      container.querySelectorAll(".composer-nudge").length,
      "a freshly opened chat screen warned about a submit nobody made",
    ).toBe(0);
    expect(
      container.querySelector('.composer [role="status"]')?.textContent ?? "",
      "the region announced a stale nudge on a screen the reader had just opened",
    ).not.toBe(EMPTY_SUBMIT_NUDGE);
  });
});

describe("both composers are SILENT but PRESENT before anything happens", () => {
  it("each has exactly one live region, and it says nothing in the idle state", () => {
    // WRITTEN BECAUSE TWO MUTANTS SURVIVED: making the hero's region
    // conditional (created together with its text, the classic way to make an
    // announcement not fire) and making it announce the sentence permanently.
    // Every other test in this file looks at the page AFTER an empty submit,
    // where a conditional region and a persistent one are indistinguishable —
    // both are there, both carry the sentence. The idle state is the only place
    // the difference is visible, and nothing was looking at it.
    // RED WHEN: either region stops being rendered while idle, or starts
    // saying something before a submit.
    for (const c of COMPOSERS) {
      const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
      c.open(container);
      const scope = container.querySelector(c.scope);
      expect(scope, `${c.label}: its scope is not on screen`).not.toBeNull();
      const regions = scope!.querySelectorAll('[role="status"]');
      expect(
        regions.length,
        `${c.label}: the live region is not in the tree before its text changes`,
      ).toBe(1);
      expect(regions[0].textContent, `${c.label}: the region is announcing while idle`).toBe("");
      // The partner: nothing is up yet, so "says nothing" is the idle state and
      // not a region that has already been and gone.
      expect(scope!.querySelectorAll(c.nudge).length, `${c.label}: a nudge before any submit`).toBe(
        0,
      );
      cleanup();
    }
  });
});

describe("the nudge outranks a refusal that is still standing", () => {
  it("announces the nudge for an empty Enter made AFTER a refusal", async () => {
    // NOT AN EQUIVALENT MUTANT, though it was reported as one. The reasoning
    // offered was that `REFUSED_SUBMIT` clears `chatNudge`, so the two are
    // mutually exclusive and the order in the view cannot be observed. That
    // covers one direction only — a refusal arriving while a nudge is up.
    //
    // The other direction is reachable and was not covered: `refusedInFlight`
    // is cleared ONLY by `SET_PENDING(0)`, so it stays true for the whole open
    // window. Refuse a question, then press Enter on an empty box a second
    // later, and both flags are true at once — the empty branch runs before the
    // in-flight gate and never touches the refusal. Order decides, and getting
    // it wrong tells the reader about a refusal from a moment ago instead of
    // the thing they just did.
    // RED WHEN: `refusedInFlight` is ordered ahead of `chatNudge` in ChatView's
    // status region.
    vi.mocked(isLiveMode).mockReturnValue(true);
    vi.mocked(createSession).mockResolvedValue({
      request_id: "r",
      session_id: "s",
      expires_at: "2026-07-11T12:00:00Z",
    });
    vi.mocked(askQuestion).mockImplementation(() => new Promise(() => {}));
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    const region = () => container.querySelector('.composer [role="status"]')?.textContent ?? "";

    const hero = container.querySelector("#hero-input") as HTMLInputElement;
    act(() => {
      fireEvent.change(hero, { target: { value: "First question" } });
      fireEvent.keyDown(hero, { key: "Enter" });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });

    const input = container.querySelector(".chat-input") as HTMLInputElement;
    // Partner: we are on the chat screen with a request genuinely open.
    expect(input, "the chat composer is not on screen").not.toBeNull();
    expect(container.querySelector(".pending-bubble")).not.toBeNull();

    // A real question, refused because one is in flight.
    act(() => {
      fireEvent.change(input, { target: { value: "Second question" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    // Partner: the refusal really is standing, so the swap below has something
    // to get wrong.
    expect(region(), "the refusal was never announced").toContain("Not sent.");

    // …and now an empty Enter, a second later. The refusal is still set,
    // because only `SET_PENDING(0)` clears it and nothing has resolved.
    act(() => {
      vi.advanceTimersByTime(1000);
      fireEvent.change(input, { target: { value: "" } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
    expect(
      region(),
      "a refusal from a moment ago outranked what the reader just did",
    ).toBe(EMPTY_SUBMIT_NUDGE);
    expect(container.querySelectorAll(".composer-nudge").length).toBe(1);
  });
});

describe("a nudge does not outlive the screen that raised it", () => {
  it("clears the HERO nudge when the screen changes", () => {
    // WRITTEN BECAUSE A MUTANT SURVIVED: `SET_SCREEN` could stop clearing
    // `heroNudge` and nothing saw it, even though the reducer comment says both
    // flags go with the screen. Its chat twin was covered; this half was not.
    // The route is the header CTA, which reaches `SET_SCREEN` through
    // `enterChat(null)` WITHOUT going through `askHero` — so this is the one
    // hero path `retireNudge("hero")` does not cover, and `SET_SCREEN` is the
    // only thing standing behind it.
    // RED WHEN: `SET_SCREEN` stops clearing `heroNudge`.
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    act(() => {
      fireEvent.click(container.querySelector(".ask-button") as HTMLElement);
    });
    // Partner: the hero nudge really is up before the screen changes.
    expect(container.querySelectorAll(".hero-nudge").length).toBe(1);

    act(() => {
      fireEvent.click(container.querySelector(".cta-button") as HTMLElement);
      vi.advanceTimersByTime(300);
    });
    act(() => {
      fireEvent.click(container.querySelector(".back-button") as HTMLElement);
      vi.advanceTimersByTime(300);
    });

    expect(
      container.querySelectorAll(".hero-nudge").length,
      "the landing page came back still warning about an ask from another screen",
    ).toBe(0);
    expect(
      container.querySelector('section.hero [role="status"]')?.textContent ?? "",
      "the hero region announced a stale nudge on a screen the reader had just returned to",
    ).toBe("");
  });
});

describe("a REAL submit retires the nudge", () => {
  const regionText = (root: HTMLElement) =>
    root.querySelector('.composer [role="status"]')?.textContent ?? "";
  const openChat = (root: HTMLElement) =>
    act(() => {
      fireEvent.click(root.querySelector(".cta-button") as HTMLElement);
    });
  const typeAndEnter = (root: HTMLElement, value: string) => {
    const input = root.querySelector(".chat-input") as HTMLInputElement;
    act(() => {
      fireEvent.change(input, { target: { value } });
      fireEvent.keyDown(input, { key: "Enter" });
    });
  };
  const emptyEnter = (root: HTMLElement) =>
    act(() => {
      fireEvent.keyDown(root.querySelector(".chat-input")!, { key: "Enter" });
    });

  it("stops saying 'Nothing was sent.' about a question that WAS sent", () => {
    // RED WHEN: `submitChat` stops retiring the nudge on the non-empty path.
    // Nothing cleared `chatNudge` on a successful send — not the 3s timer, not
    // `REFUSED_SUBMIT`, not `SET_SCREEN` — so for up to three seconds after an
    // empty Enter, a real question sent inside that window left the region
    // reading the empty-submit sentence while the question sat in the
    // transcript streaming, AND suppressed that question's own "Searching the
    // docs…" and "Answer ready." announcements. Same class of false statement
    // to an AT user as #445 and #356 gap 2.
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    openChat(container);
    emptyEnter(container);
    // Partner: the nudge really is up, so its absence below is a retirement and
    // not a submit that never registered.
    expect(regionText(container)).toBe(EMPTY_SUBMIT_NUDGE);
    expect(container.querySelectorAll(".composer-nudge").length).toBe(1);

    typeAndEnter(container, "What is Claude Code?");
    act(() => {
      vi.advanceTimersByTime(200);
    });

    // Partner: the question really was sent, so what follows is about a live
    // send and not about a submit the app dropped.
    expect(sentAnything(container), "the question never reached the transcript").toBe(true);
    expect(
      regionText(container),
      "the region told a screen reader nothing was sent, about a question that was",
    ).not.toBe(EMPTY_SUBMIT_NUDGE);
    expect(
      container.querySelectorAll(".composer-nudge").length,
      "the empty-box warning stayed on screen over a question that was sent",
    ).toBe(0);
  });

  it("retires it on the DUPLICATE path too, which never bumps the send tick", () => {
    // The reason the anchor is `submitChat` and not the `BUMP_SEND_TICK`
    // reducer case, which was the first fix proposed. `send()` returns early on
    // `flashExisting(existing)` for a question that was ALREADY ANSWERED
    // (useLandingState.ts, the duplicate guard), and that branch dispatches no
    // tick at all — so a tick-anchored clear leaves exactly the same false
    // announcement on a path any user reaches by re-asking something.
    // RED WHEN: the retirement moves to `BUMP_SEND_TICK`, or off the non-empty
    // path in `submitChat`.
    const { container } = render(<LandingPage theme="light" onThemeChange={() => {}} />);
    openChat(container);
    typeAndEnter(container, "What is Claude Code?");
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    const answered = container.querySelectorAll(".message.user-msg").length;
    expect(answered, "the first question was never asked").toBe(1);

    emptyEnter(container);
    expect(regionText(container)).toBe(EMPTY_SUBMIT_NUDGE);

    // The SAME question again -> the duplicate guard flashes the existing
    // bubble and returns, adding no message and bumping no tick.
    typeAndEnter(container, "What is Claude Code?");
    act(() => {
      vi.advanceTimersByTime(200);
    });
    // Partner proving this really took the duplicate path: no second bubble.
    expect(
      container.querySelectorAll(".message.user-msg").length,
      "this was not the duplicate path — a second bubble was added",
    ).toBe(answered);
    expect(regionText(container), "the duplicate path left the false sentence up").not.toBe(
      EMPTY_SUBMIT_NUDGE,
    );
    expect(container.querySelectorAll(".composer-nudge").length).toBe(0);
  });
});

describe("the two nudges share one stylesheet rule", () => {
  it("one declaration block carries BOTH selectors, and it is not empty", () => {
    // WRITTEN BECAUSE A MUTANT SURVIVED: `.composer-nudge` could be dropped
    // from the shared block entirely — leaving the chat warning unstyled, with
    // none of the amber, the size or the fade — and every behavioural test in
    // this file stayed green, because jsdom renders no stylesheet and the guard
    // reads the DOM, not the paint.
    //
    // Parsed with postcss rather than grepped, the way
    // `tokenThemeParity.test.ts` does: a regex over the file text cannot tell a
    // selector from the same characters inside a comment, and the comments
    // around this rule quote both selectors several times.
    //
    // WHAT THIS DOES NOT PROVE, said plainly because the comment it replaces
    // claimed the pair "cannot be restyled apart" and that was false: a later
    // or more specific rule still overrides one of them, and nothing here
    // compares the two COMPUTED colours. What is guaranteed is that this block
    // reaches both. The rendered result is
    // `frontend/tests/contrast-floor.spec.ts`, which measures each nudge in a
    // real browser.
    // RED WHEN: either selector is dropped from the shared rule, they are split
    // into two blocks, or the block is emptied.
    const HERE = dirname(fileURLToPath(import.meta.url));
    const path = join(HERE, "..", "styles", "landing.css");
    const css = readFileSync(path, "utf8");

    const norm = (s: string) => s.replace(/\s+/g, " ").trim();
    const carrying: Rule[] = [];
    postcss.parse(css, { from: path }).walkRules((rule: Rule) => {
      const selectors = rule.selectors.map(norm);
      if (selectors.includes(".hero-nudge") || selectors.includes(".composer-nudge")) {
        carrying.push(rule);
      }
    });

    // The partner. "Every rule mentioning a nudge also mentions the other" is
    // vacuously true when NO rule mentions either — which is what a rename, a
    // deleted block or a broken parse produces.
    expect(carrying.length, "no rule in landing.css styles either nudge").toBe(1);
    const [shared] = carrying;
    expect(shared.selectors.map(norm).sort()).toEqual([".composer-nudge", ".hero-nudge"]);
    // …and the one rule they share actually declares something. A shared empty
    // block satisfies every assertion above and styles neither.
    const declared = shared.nodes.filter((n) => n.type === "decl").length;
    expect(declared, "the shared block declares nothing").toBeGreaterThan(0);
  });
});

describe("the parity guard covers every composer it lists", () => {
  it("generated tests for both, not just the first", () => {
    // The `continue` -> `break` survivor. The loop above generates a `describe`
    // per composer at collection time; swap its `for` for one that stops after
    // the first entry and every assertion still standing is green, because the
    // chat composer's tests simply cease to exist. A suite cannot fail a test it
    // never registered, so the registry is checked instead.
    // RED WHEN: the generator loop breaks, returns, or is filtered early.
    expect(EXERCISED).toEqual(COMPOSERS.map((c) => c.label));
    expect(EXERCISED).toEqual(["hero", "chat"]);
  });
});
