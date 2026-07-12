/**
 * useRevealOnScroll
 *
 * Tiny IntersectionObserver-backed hook. Scans the element on mount
 * for descendants with the ``soft-reveal`` class and adds
 * ``is-visible`` once they enter the viewport. Falls back to
 * immediately revealing everything when IntersectionObserver is not
 * available (older test environments) or when the user prefers
 * reduced motion.
 */

import { useEffect, useRef } from "react";

const REVEAL_CLASS = "soft-reveal";
const VISIBLE_CLASS = "is-visible";
const REVEALED_ATTR = "data-soft-revealed";

export function useRevealOnScroll<T extends Element = HTMLDivElement>() {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    const root = ref.current;
    if (!root) return;

    const prefersReduced = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;
    if (prefersReduced || typeof IntersectionObserver === "undefined") {
      root.querySelectorAll(`.${REVEAL_CLASS}`).forEach((el) => {
        if (!el.classList.contains(VISIBLE_CLASS)) {
          el.classList.add(VISIBLE_CLASS);
          el.setAttribute(REVEALED_ATTR, "true");
        }
      });
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add(VISIBLE_CLASS);
            entry.target.setAttribute(REVEALED_ATTR, "true");
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
    );

    const elements = Array.from(
      root.querySelectorAll<HTMLElement>(`.${REVEAL_CLASS}`)
    );
    elements.forEach((element) => observer.observe(element));

    return () => observer.disconnect();
  }, []);

  return ref;
}
