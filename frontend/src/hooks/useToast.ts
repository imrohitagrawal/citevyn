import { useCallback, useEffect, useRef, useState } from "react";

export interface Toast {
  id: string;
  kind: "info" | "success" | "warning" | "error";
  title: string;
  message: string;
}

const DISMISS_AFTER_MS = 5000;

/**
 * How many toast cards may be on screen at once. `ToastHost` is a
 * `position: fixed` column with no scroll, so cards past this point render
 * above the top of the viewport where they can be neither read nor dismissed.
 * Three fits the 129 px card this app actually produces in a 900 px viewport
 * with room to spare.
 */
const MAX_VISIBLE_TOASTS = 3;

export function useToast() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const counter = useRef(0);
  const pending = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  const addToast = useCallback((toast: Omit<Toast, "id">) => {
    const id = `toast-${++counter.current}`;
    setToasts((prev) => {
      // COLLAPSE an identical toast rather than stacking it, and re-arm its
      // timer by giving it a new id. Measured in a real browser: ten rapid
      // clicks on one landing question with a draft in the composer produced
      // ten byte-identical cards — a 1,385 px column in a 900 px viewport, with
      // FOUR of them above the top of the screen and therefore unreachable and
      // undismissable, covering the right-hand column for five seconds.
      const rest = prev.filter(
        (t) =>
          !(t.kind === toast.kind && t.title === toast.title && t.message === toast.message),
      );
      // Hard cap for DISTINCT toasts, which dedupe cannot help with (an error
      // storm on a flaky connection is the realistic case). `ToastHost` is
      // `position: fixed` with no scroll, so anything past a few cards is off
      // screen. Oldest goes first: the newest is the one the reader just
      // caused. The auto-dismiss effect below drops the timer for any toast
      // that leaves the list, so this cannot leak.
      return [...rest, { ...toast, id }].slice(-MAX_VISIBLE_TOASTS);
    });
  }, []);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  /**
   * Auto-dismiss is armed HERE, from rendered state, rather than inside
   * `addToast`.
   *
   * The obvious version — `setTimeout` inside `addToast`, cancelled by an
   * unmount effect — is wrong in two ways that both bit us:
   *
   *   1. React StrictMode (see `main.tsx`) runs every effect setup, then
   *      cleanup, then setup again on mount. State survives that simulated
   *      remount but the cleared timers do not, so a toast added by a mount
   *      effect — which is exactly what the `?auth=ok` / `?connect=ok` return
   *      trips in `LandingPage` do — kept its state and lost its timer, and sat
   *      on screen forever in `npm run dev`. Arming from state means the very
   *      next render re-arms anything that lost its timer.
   *   2. An `addToast` that lands AFTER unmount (any of the awaited network
   *      paths, e.g. `handleApiError`) would schedule a timer nothing could
   *      ever cancel. React drops the state update on an unmounted tree, so
   *      no render happens, so nothing is armed. The problem cannot occur.
   *
   * Why it matters beyond a stuck pixel: an uncancelled timer calls `setToasts`
   * after vitest has torn the test environment down, and vitest then exits 1
   * while reporting every test as PASSED — the required `type-check + unit
   * tests + build` check going red with a green-looking report. Reproduced on
   * unmodified `main` in 1 of 6 full-suite runs while investigating #344.
   */
  useEffect(() => {
    for (const toast of toasts) {
      if (pending.current.has(toast.id)) continue;
      pending.current.set(
        toast.id,
        setTimeout(() => {
          pending.current.delete(toast.id);
          setToasts((prev) => prev.filter((t) => t.id !== toast.id));
        }, DISMISS_AFTER_MS),
      );
    }
    // Drop timers for toasts that are already gone — dismissed by hand through
    // `removeToast`, say — so the map cannot grow across a long session.
    const live = new Set(toasts.map((t) => t.id));
    for (const [id, timer] of pending.current) {
      if (live.has(id)) continue;
      clearTimeout(timer);
      pending.current.delete(id);
    }
  }, [toasts]);

  // Real unmount: nothing is left queued to fire into a tree that is gone.
  // Under StrictMode this also runs on the simulated remount, which is safe
  // precisely because the effect above re-arms from state on the next render.
  useEffect(() => {
    const timers = pending.current;
    return () => {
      timers.forEach(clearTimeout);
      timers.clear();
    };
  }, []);

  return { toasts, addToast, removeToast };
}
