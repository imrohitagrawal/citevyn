/**
 * useDeferredReveal (#358) — "mount this once the reader is heading toward it".
 *
 * Backs the lazy below-the-fold marketing strip. Two ways to become revealed,
 * and once revealed it never un-reveals:
 *
 *   1. The sentinel element comes within `rootMargin` of the viewport.
 *   2. Something calls `reveal()` — the header nav and the on-load `#hash`
 *      handler do, because their scroll target lives inside the deferred
 *      content and `scrollToSection` cannot scroll to an element that is not
 *      there.
 *
 * FAILS OPEN, deliberately. If the environment has no IntersectionObserver the
 * hook starts revealed, so the content is present rather than invisible
 * forever. That covers jsdom (which does not implement IntersectionObserver at
 * all — an unguarded `new IntersectionObserver()` throws a ReferenceError and
 * takes every unit test that renders LandingPage with it) and any browser old
 * enough to lack it. The same guard shape is used by src/lib/useRevealOnScroll.ts.
 *
 * "Cannot observe" must mean "show it", never "hide it".
 *
 * WHY A CALLBACK REF AND NOT A useEffect OVER A RefObject.
 * The first version took a `RefObject` and attached the observer in a
 * `useEffect` keyed on `[revealed, rootMargin, sentinel]`. Every one of those
 * is stable across a landing -> chat -> landing round trip: `sentinel` is a
 * useRef object, `rootMargin` is a default string, and `revealed` is still
 * false. LandingPage unmounts <main> for the chat screen, so the sentinel node
 * is destroyed and recreated — but the effect never re-ran, and the observer
 * went on watching the DETACHED original node, which can never intersect. The
 * result was real content loss: on a phone, visiting the chat and coming back
 * left the whole marketing strip unreachable by scrolling for the rest of the
 * session. React calls a CALLBACK ref with null on unmount and with the new
 * node on remount, so the observer follows the node instead of a stale
 * dependency list.
 */
import { useCallback, useRef, useState } from "react";

/** How far ahead of the viewport to start fetching. See the note in LandingPage. */
export const STRIP_ROOT_MARGIN = "400px 0px";

export function useDeferredReveal(rootMargin: string = STRIP_ROOT_MARGIN): {
  revealed: boolean;
  reveal: () => void;
  sentinelRef: (node: Element | null) => void;
} {
  // Computed during the FIRST render, not in an effect: a jsdom test that
  // asserts synchronously after render() must already see `true`.
  const [revealed, setRevealed] = useState(() => typeof IntersectionObserver === "undefined");
  const active = useRef<IntersectionObserver | null>(null);

  const reveal = useCallback(() => setRevealed(true), []);

  const sentinelRef = useCallback(
    (node: Element | null) => {
      // Always tear down first. React invokes this with null before handing
      // over a new node, and again when `revealed` changes the callback's
      // identity, so this is the single place the observer is released.
      active.current?.disconnect();
      active.current = null;

      if (node === null || revealed) return;
      if (typeof IntersectionObserver === "undefined") return;

      try {
        const observer = new IntersectionObserver(
          (entries) => {
            if (!entries.some((e) => e.isIntersecting)) return;
            observer.disconnect();
            setRevealed(true);
          },
          { rootMargin },
        );
        observer.observe(node);
        active.current = observer;
      } catch {
        // The constructor THROWS on a malformed rootMargin — verified in
        // Chromium: "400" and "400 px 0px" both raise SyntaxError. Guarding
        // only `typeof IntersectionObserver === "undefined"` would let that
        // throw escape a ref callback and take the page down, which is the
        // exact opposite of this file's stated invariant. Fail open: show the
        // content, same as having no observer at all.
        setRevealed(true);
      }
    },
    [revealed, rootMargin],
  );

  return { revealed, reveal, sentinelRef };
}
