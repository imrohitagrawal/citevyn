/**
 * The header nav and the deferred-section set, in ONE place (#358).
 *
 * WHY THIS FILE EXISTS. These were two hand-maintained string lists in two
 * components: the nav ids inline in Header.tsx, and DEFERRED_SECTION_IDS inline
 * in LandingPage.tsx. Nothing related them, and review proved the gap by adding
 * a nav link pointing at a section inside the lazy strip WITHOUT adding its id
 * to the set: clicking it mounted nothing and scrolled nowhere, `tsc -b` exited
 * 0, and the e2e nav loop stayed green because it hardcoded the four labels it
 * already knew about. That is exactly the dead-link regression #358 exists to
 * remove, reintroduced by a one-line edit somewhere else.
 *
 * Now the nav renders FROM this list and the tests iterate it, so a new entry is
 * exercised the moment it is added.
 */

export interface NavSection {
  /** The section's DOM id, and the link's href fragment. */
  id: string;
  /** Link text in the header. */
  label: string;
  /**
   * True when the section lives in the lazily-loaded landing strip, so a click
   * has to reveal the strip before `scrollToSection` can find the target.
   * False for sections that are always in the eager bundle.
   */
  deferred: boolean;
}

/** The header nav, in render order. */
export const NAV_SECTIONS: readonly NavSection[] = [
  { id: "who", label: "Who it's for", deferred: true },
  { id: "how", label: "How it works", deferred: true },
  // InteractiveDemo deliberately stays eager (#358), so its anchor is always
  // in the DOM and clicking it must NOT drag the strip down.
  { id: "demo", label: "Demo", deferred: false },
  { id: "pricing", label: "Pricing", deferred: true },
  { id: "faq", label: "FAQ", deferred: true },
];

/**
 * Sections inside the lazy strip that have NO nav link but are still reachable
 * by deep link (`/#why`). Kept separate from NAV_SECTIONS so the nav stays the
 * nav; both feed DEFERRED_SECTION_IDS below.
 */
const DEFERRED_WITHOUT_NAV = ["why"] as const;

/**
 * Every section id that lives in the deferred strip. A nav click or an on-load
 * `#hash` naming one of these must reveal the strip first — `scrollToSection`
 * cannot scroll to an element that has not been fetched yet.
 */
export const DEFERRED_SECTION_IDS: ReadonlySet<string> = new Set<string>([
  ...NAV_SECTIONS.filter((s) => s.deferred).map((s) => s.id),
  ...DEFERRED_WITHOUT_NAV,
]);
