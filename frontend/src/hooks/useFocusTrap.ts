import { useEffect, type RefObject } from "react";

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
 *     TAB #2  BUTTON "Claude Code"     inDialog: true
 *     TAB #3  BUTTON "Pricing"         inDialog: true
 *     TAB #4  BODY                     inDialog: false
 *     TAB #5  A "Docs"                 inDialog: false
 *     Shift+Tab from the dialog -> BUTTON "me@example.com"   (ONE press)
 *
 * So `aria-modal="true"` told assistive technology the background was inert
 * and the backdrop made those controls unclickable by mouse, while the keyboard
 * reached and could activate them. Per the ARIA Authoring Practices, a dialog
 * with `aria-modal="true"` must confine Tab.
 *
 * Extracted from `AuthModal`'s implementation rather than written afresh: that
 * one was already mutation-guarded in both wrap directions, and a third
 * hand-rolled copy is how the two halves drift apart.
 */

/**
 * Deliberately no `offsetParent`/layout visibility filter. `offsetParent` is
 * always null under jsdom (no layout engine), which silently emptied this list
 * and disabled the trap under test — found by AuthModal's own trap tests. If a
 * dialog ever hides a control conditionally, it should not render it.
 */
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

type Options = {
  /**
   * Close handler for Escape. Omit to leave Escape alone.
   */
  onEscape?: () => void;
  /**
   * Set false to stand the trap down while a dialog is stacked ON TOP of this
   * one — `ConnectedAccountsDrawer` opens `AuthModal` over itself, and two
   * document-level traps would otherwise fight over focus, the drawer yanking
   * it back out of the modal on every Tab. The drawer's Escape handler already
   * stands down the same way, for the same reason.
   */
  enabled?: boolean;
};

export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  { onEscape, enabled = true }: Options = {},
) {
  useEffect(() => {
    if (!enabled) return;

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && onEscape) {
        e.preventDefault();
        onEscape();
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
      // on the account button behind the backdrop, which is exactly the
      // reported defect.
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
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [containerRef, onEscape, enabled]);
}
