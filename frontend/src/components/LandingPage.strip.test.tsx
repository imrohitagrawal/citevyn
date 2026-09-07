import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LandingPage } from "./LandingPage";
import { isLiveMode } from "../lib/api";
import { NAV_SECTIONS, DEFERRED_SECTION_IDS } from "../data/navSections";

/**
 * #358 — the deferred below-the-fold marketing strip, from the READER's side.
 *
 * The bundle-side proof lives in scripts/lazy-landing-strip.test.mjs, which reads
 * the emitted chunks. This file proves the other half: that deferring the
 * sections did not make them unreachable. Every regression the split could
 * introduce is a way for real content to go missing, so each one gets a test:
 *
 *   - the sections still arrive when the reader scrolls (the observer fires);
 *   - the header nav still works, even though four of its five links now point
 *     at content that is not in the DOM when the link is clicked;
 *   - a deep link (`/#pricing`) still lands somewhere real;
 *   - a browser with no IntersectionObserver gets the whole page, not a stub.
 *
 * jsdom implements no IntersectionObserver at all, so these tests INSTALL one.
 * Without that stub the hook fails open and the strip is always present, which
 * would make every assertion below pass for the wrong reason — see the
 * "fails open" test at the bottom, which is the one case that wants the real
 * (absent) environment.
 */
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

type ObserverCallback = (entries: Array<{ isIntersecting: boolean }>) => void;

/** Captures the observer LandingPage creates so a test can fire it by hand. */
function installObserver() {
  const fire: ObserverCallback[] = [];
  class FakeIntersectionObserver {
    constructor(cb: ObserverCallback) {
      fire.push(cb);
    }
    observe() {}
    disconnect() {}
    unobserve() {}
    takeRecords() {
      return [];
    }
  }
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  return {
    /** Simulate the sentinel coming into view. */
    scrollIntoView() {
      act(() => {
        for (const cb of fire) cb([{ isIntersecting: true }]);
      });
    },
  };
}

/** Text that only exists inside the deferred strip. */
const FAQ_HEADING = "Questions, answered.";
const FOOTER_COPY = /Answers from official docs only\./;

beforeEach(() => {
  vi.mocked(isLiveMode).mockReturnValue(false);
  window.history.replaceState(null, "", "/");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function renderPage() {
  return render(<LandingPage theme="light" onThemeChange={() => {}} />);
}

describe("the deferred landing strip (#358)", () => {
  it("stays out of the DOM until something reveals it, while the at-the-fold sections render", async () => {
    // WARM THE LAZY MODULE FIRST. React caches a resolved lazy module for the
    // life of the module instance (the scar documented in
    // ConnectedAccountsDrawer.coldStart.test.tsx), and that cache is what makes
    // this test mean anything. Asserting absence on a COLD module passes for
    // the wrong reason: React.lazy suspends on first render, so
    // `fallback={null}` renders nothing whether the gate is open or shut.
    // Mutation-tested — with a cold module, replacing `stripRevealed &&` with
    // `true &&` SURVIVED. Once the module is warm, React renders it
    // synchronously the moment it is mounted, so "still not here" can only mean
    // the gate held it back.
    const warm = installObserver();
    const first = renderPage();
    warm.scrollIntoView();
    await screen.findByText(FAQ_HEADING);
    first.unmount();
    cleanup();

    installObserver();
    const { container } = renderPage();
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.queryByText(FAQ_HEADING)).not.toBeInTheDocument();
    expect(container.querySelector("#pricing")).toBeNull();
    expect(container.querySelector("#who")).toBeNull();
    expect(container.querySelector("footer")).toBeNull();

    // PARTNER for those absences: the page really did render, and the sections
    // that must survive first paint did. Without this, deleting LandingPage's
    // whole <main> would pass the assertions above.
    expect(container.querySelector(".sources-strip")).not.toBeNull();
    expect(container.querySelector("#demo")).not.toBeNull();
  });

  it("arrives when the reader scrolls toward it", async () => {
    const observer = installObserver();
    const { container } = renderPage();
    expect(container.querySelector("#pricing")).toBeNull();

    observer.scrollIntoView();

    expect(await screen.findByText(FAQ_HEADING)).toBeInTheDocument();
    expect(container.querySelector("#who")).not.toBeNull();
    expect(container.querySelector("#how")).not.toBeNull();
    expect(container.querySelector("#why")).not.toBeNull();
    expect(container.querySelector("#pricing")).not.toBeNull();
    expect(screen.getByText(FOOTER_COPY)).toBeInTheDocument();
  });

  it("renders the deferred sections in their original DOM order around the eager demo", async () => {
    const observer = installObserver();
    const { container } = renderPage();
    observer.scrollIntoView();
    await screen.findByText(FAQ_HEADING);

    const ids = [...container.querySelectorAll("main > section[id], main > *[id]")]
      .map((el) => el.id)
      .filter(Boolean);
    // The eager InteractiveDemo must still sit BETWEEN the two deferred runs.
    expect(ids).toEqual(["who", "how", "why", "demo", "pricing", "faq"]);
  });

  // DERIVED from the shared nav list, never hardcoded. Review proved the
  // hardcoded version's gap: adding a nav link that points into the strip
  // without adding its id to DEFERRED_SECTION_IDS produced a dead link, `tsc`
  // exited 0, and this loop stayed green because it only knew the four labels
  // it was written with. Iterating the source means a new entry is exercised
  // the moment someone adds it.
  it.each(NAV_SECTIONS.filter((s) => s.deferred).map((s) => [s.label, s.id] as const))(
    "mounts AND scrolls to the target when the header's %s link is clicked",
    async (label, id) => {
    installObserver();
    // jsdom does not implement scrollTo; stub it so the second half of the
    // click — actually moving the page — is observable and not just assumed.
    const scrollTo = vi.fn();
    vi.stubGlobal("scrollTo", scrollTo);
    const user = userEvent.setup();
    const { container } = renderPage();
    expect(container.querySelector(`#${id}`)).toBeNull();

    await user.click(screen.getByRole("link", { name: label }));

    // Half one: the target actually appears.
    await waitFor(() => expect(container.querySelector(`#${id}`)).not.toBeNull());
    // Half two: and the page scrolls to it. Before #358 `scrollToSection` gave
    // up the instant the element was missing, so the link mounted nothing and
    // moved nothing — it just looked dead. Mounting alone would still leave the
    // reader stranded at the top of the page.
    await waitFor(() => expect(scrollTo).toHaveBeenCalled());
    },
  );

  it("covers every nav link, so neither derived loop can silently shrink", () => {
    // PARTNER for both derived it.each blocks: a filter that matched nothing
    // would run ZERO tests and the file would still be green. Review proved the
    // eager half open — deleting the one `deferred: false` entry removed the
    // header link, ran zero eager tests, and left everything green.
    const deferred = NAV_SECTIONS.filter((s) => s.deferred);
    const eager = NAV_SECTIONS.filter((s) => !s.deferred);
    expect(deferred.length).toBeGreaterThanOrEqual(4);
    expect(eager.length).toBeGreaterThanOrEqual(1);

    // And the split matches what LandingPage actually consults, both ways: a
    // deferred id missing from the set is a DEAD LINK, an eager id inside it
    // costs a pointless reveal.
    for (const s of deferred) expect(DEFERRED_SECTION_IDS.has(s.id)).toBe(true);
    for (const s of eager) expect(DEFERRED_SECTION_IDS.has(s.id)).toBe(false);
  });

  it("renders one header link per nav entry, in order", () => {
    installObserver();
    renderPage();

    // Pins the nav itself, not just the click behaviour. Without this, deleting
    // an entry from NAV_SECTIONS removes a link from the product and every
    // derived loop simply runs one fewer case.
    const labels = screen
      .getAllByRole("link")
      .map((a) => a.textContent?.trim())
      .filter((t) => NAV_SECTIONS.some((s) => s.label === t));
    expect(labels).toEqual(NAV_SECTIONS.map((s) => s.label));
  });

  it.each(NAV_SECTIONS.filter((s) => !s.deferred).map((s) => [s.label, s.id] as const))(
    "leaves the strip alone for the eager %s link",
    async (label, id) => {
    installObserver();
    const user = userEvent.setup();
    const { container } = renderPage();

    await user.click(screen.getByRole("link", { name: label }));

    // The eager target is already there — no fetch needed...
    expect(container.querySelector(`#${id}`)).not.toBeNull();
    // ...and clicking it must not drag 4.7 kB of marketing copy down with it.
    await new Promise((r) => setTimeout(r, 0));
    expect(container.querySelector("#pricing")).toBeNull();
    },
  );

  it("mounts the strip for a deep link that names one of its sections", async () => {
    installObserver();
    window.history.replaceState(null, "", "/#pricing");

    const { container } = renderPage();

    await waitFor(() => expect(container.querySelector("#pricing")).not.toBeNull());
  });

  it("fails OPEN: with no IntersectionObserver the whole page renders", async () => {
    // No installObserver() here — this is jsdom's real environment, and it is
    // also every browser too old to have the API. "Cannot observe" must mean
    // "show the content".
    vi.stubGlobal("IntersectionObserver", undefined);

    const { container } = renderPage();

    expect(await screen.findByText(FAQ_HEADING)).toBeInTheDocument();
    expect(container.querySelector("#pricing")).not.toBeNull();
    expect(container.querySelector("footer")).not.toBeNull();
  });
});
