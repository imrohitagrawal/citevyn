import { useEffect, useRef, type RefObject } from "react";
import { isTopTrap, pushTrap, removeTrap } from "../lib/dialogStack";

/**
 * Confine Tab and Shift+Tab to a dialog, and close it on Escape.
 *
 * WHY THIS EXISTS (#331). `AuthModal`, `HistoryDrawer` and
 * `ConnectedAccountsDrawer` all render `role="dialog"` with
 * `aria-modal="true"`, and all three put a backdrop over the page. Only
 * `AuthModal` actually trapped Tab. Both drawers documented the omission as
 * *"no form fields, so no Tab trap"* — but the hazard is escaping the dialog,
 * not moving between inputs. Measured in real Chromium against the drawer's
 * rendered DOM before this hook existed:
 *
 *     TAB #1  BUTTON "Close"           inDialog: true
 *     TAB #2  BUTTON "claude_code..."  inDialog: true
 *     TAB #3  BODY                     inDialog: false
 *     TAB #4  A "CiteVyn01"            inDialog: false
 *     Shift+Tab from the dialog -> BUTTON "Ask your first question"  (ONE press)
 *
 * So `aria-modal="true"` told assistive technology the background was inert
 * and the backdrop made those controls unclickable by mouse, while the keyboard
 * reached and could activate them. Per the ARIA Authoring Practices, a dialog
 * with `aria-modal="true"` must confine Tab.
 *
 * Extracted from `AuthModal`'s implementation rather than written afresh: that
 * one was already guarded in both wrap directions, and a third hand-rolled copy
 * is how the halves drift apart.
 */

/**
 * Deliberately no `offsetParent`/layout visibility filter. `offsetParent` is
 * always null under jsdom (no layout engine), which silently emptied this list
 * and disabled the trap under test — found by AuthModal's own trap tests. If a
 * dialog ever hides a control conditionally, it should not render it.
 */
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * The mount-ordered stack lives in `../lib/dialogStack`, on its own, because
 * `useLandingState` (eager chunk) needs `isModalDialogOpen()` while this hook
 * is only ever loaded inside lazy chunks. Importing the hook from there pulled
 * the whole trap into the eager bundle — measured 65,540 -> 65,849 B gzip
 * against a 66,000 B budget.
 *
 * ONLY THE TOP-MOST mounted dialog acts. This replaced an `enabled` flag that
 * `ConnectedAccountsDrawer` drove from its own `passwordOpen` state, and review
 * showed why that was the wrong seam: `AuthModal` is `React.lazy`, so on the
 * FIRST "Set a password" of a page load `passwordOpen` flips — standing the
 * drawer's trap down — while the modal's chunk is still being fetched and its
 * replacement trap does not yet exist. Measured in that window: a Tab
 * dispatched from a page button behind the backdrop was NOT prevented and
 * focus stayed outside, and Escape did nothing. A trap that hands off to
 * something not yet mounted is a hole, not a handoff.
 *
 * Keyed on mount rather than on a flag, the drawer simply stays topmost until
 * the modal actually mounts, and defers the moment it does.
 */

type Options = {
  /** Close handler for Escape. Omit to leave Escape alone. */
  onEscape?: () => void;
};

export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  { onEscape }: Options = {},
) {
  // Held in a ref so the listener effect can have EMPTY deps. All three call
  // sites pass an inline arrow for `onClose`, so a dependency on it would
  // re-subscribe on every parent render — and, worse, would re-order the stack
  // above, silently promoting a background dialog over the one on top of it.
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  });

  useEffect(() => {
    const token = {};
    pushTrap(token);

    const handleKeyDown = (e: KeyboardEvent) => {
      // Only the top-most mounted dialog reacts. Everything below it is inert,
      // which is exactly what its own `aria-modal="true"` already claims.
      if (!isTopTrap(token)) return;

      if (e.key === "Escape" && onEscapeRef.current) {
        e.preventDefault();
        onEscapeRef.current();
        return;
      }
      if (e.key !== "Tab") return;

      const container = containerRef.current;
      if (!container) return;

      const focusable = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) {
        // Nothing to move to, but Tab must still not leave the dialog.
        e.preventDefault();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;

      // Focus is not on one of the dialog's own focusable controls — it is on
      // the dialog element itself (the drawers take focus via `tabIndex={-1}`),
      // or it has escaped to the page behind the backdrop. Pull it back in.
      // Without this branch, ONE Shift+Tab from a freshly-opened drawer landed
      // on a page button, which is exactly the reported defect.
      if (!(active instanceof HTMLElement) || !focusable.includes(active)) {
        e.preventDefault();
        (e.shiftKey ? last : first).focus();
        return;
      }

      // Wrap at both ends — this is what makes it a TRAP rather than an
      // initial-focus courtesy. A trap that only wraps forward is the usual
      // half-fix.
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      removeTrap(token);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [containerRef]);
}
