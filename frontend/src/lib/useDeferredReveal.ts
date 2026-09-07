/**
 * useDeferredReveal (#358) — "mount this once the reader is heading toward it".
 *
 * Backs the lazy below-the-fold marketing strip. Two ways to become revealed,
 * and once revealed it never un-reveals:
 *
 *   1. The sentinel element comes within `rootMargin` of the viewport.
 *   2. Something calls `reveal()` — the header nav and the on-load `#hash`
 *      handler do, because their scroll target lives inside the deferred
 *      content and `scrollToId` cannot scroll to an element that is not there.
 *
 * FAILS OPEN, deliberately. If the environment has no IntersectionObserver the
 * hook starts revealed, so the content is present rather than invisible
 * forever. That covers jsdom (which does not implement IntersectionObserver at
 * all — an unguarded `new IntersectionObserver()` throws a ReferenceError and
 * takes every unit test that renders LandingPage with it) and any browser old
 * enough to lack it. The same guard shape is used by src/lib/useRevealOnScroll.ts.
 *
 * "Cannot observe" must mean "show it", never "hide it".
 */
import { useCallback, useEffect, useState, type RefObject } from "react";

/** How far ahead of the viewport to start fetching. See the note in LandingPage. */
export const STRIP_ROOT_MARGIN = "400px 0px";

export function useDeferredReveal(
  sentinel: RefObject<Element | null>,
  rootMargin: string = STRIP_ROOT_MARGIN,
): { revealed: boolean; reveal: () => void } {
  // Computed during the FIRST render, not in an effect: a jsdom test that
  // asserts synchronously after render() must already see `true`.
  const [revealed, setRevealed] = useState(() => typeof IntersectionObserver === "undefined");

  const reveal = useCallback(() => setRevealed(true), []);

  useEffect(() => {
    if (revealed) return;
    const el = sentinel.current;
    // No sentinel on screen (the chat view, say) — nothing to observe, and
    // nothing to reveal either. Re-runs when `revealed` or the margin changes.
    if (!el) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((e) => e.isIntersecting)) return;
        observer.disconnect();
        setRevealed(true);
      },
      { rootMargin },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [revealed, rootMargin, sentinel]);

  return { revealed, reveal };
}
