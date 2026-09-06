import { describe, it, expect, afterEach, vi } from "vitest";
import { StrictMode, useEffect } from "react";
import { renderHook, act } from "@testing-library/react";
import { useToast } from "./useToast";

/**
 * A toast schedules a 5 s auto-dismiss timer. Nothing used to cancel it, so a
 * tree unmounted inside that window left the callback queued to call
 * `setToasts` on a component that was already gone.
 *
 * The visible damage is in the test runner rather than the browser: the
 * callback fires after vitest has torn the environment down, and vitest exits
 * 1 while reporting every test as passed. That is `type-check + unit tests +
 * build` — a REQUIRED check — going red with a green-looking report. Observed
 * on unmodified `main` in 1 of 6 full-suite runs during the #344 work.
 *
 * The first fix armed the timer in `addToast` and cancelled it in an unmount
 * effect. A reviewer measured that this breaks under StrictMode, so the timer
 * is now armed from rendered state. Each test below names the change that
 * turns it red.
 */
describe("useToast auto-dismiss timers", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  // RED if the unmount cleanup in useToast.ts is removed: the count stays 1.
  it("leaves no timer queued after the tree goes away", () => {
    vi.useFakeTimers();
    const { result, unmount } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ kind: "info", title: "Saved", message: "All good." });
    });

    // THE PARTNER ASSERTION: a timer really was scheduled, so "0 timers after
    // unmount" cannot pass just because nothing was ever queued.
    expect(vi.getTimerCount()).toBe(1);
    expect(result.current.toasts).toHaveLength(1);

    unmount();

    expect(vi.getTimerCount()).toBe(0);
  });

  // RED if the arming effect stops dismissing (or never arms): the toast stays.
  // This is what stops the cleanup above from being "delete auto-dismiss".
  it("still auto-dismisses while the tree is alive", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ kind: "info", title: "Saved", message: "All good." });
    });
    expect(result.current.toasts).toHaveLength(1);

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current.toasts).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  // RED if the timer is armed inside `addToast` instead of from rendered state.
  //
  // StrictMode runs effect setup -> cleanup -> setup on mount. State survives
  // that simulated remount; cleared timers do not. `LandingPage` adds a toast
  // from a mount effect on the `?auth=ok` / `?connect=ok` return trips, so with
  // arm-in-addToast the toast kept its state, lost its timer, and sat on screen
  // forever in `npm run dev`. Measured by a reviewer as 2 toasts / 1 timer.
  //
  // The wrapper mirrors that shape: useToast first, then an effect that toasts.
  it("re-arms a toast added from a mount effect, under StrictMode", () => {
    vi.useFakeTimers();
    const seen: { toasts: ReturnType<typeof useToast>["toasts"] } = { toasts: [] };

    function Probe() {
      const { toasts, addToast } = useToast();
      seen.toasts = toasts;
      useEffect(() => {
        addToast({ kind: "success", title: "Signed in", message: "Welcome." });
      }, [addToast]);
      return null;
    }

    act(() => {
      renderHook(() => null, {
        wrapper: () => (
          <StrictMode>
            <Probe />
          </StrictMode>
        ),
      });
    });

    // Partner: StrictMode really did double-invoke, so this is the hostile
    // case and not a quiet single-mount run.
    //
    // This used to assert `toasts.length === 2`, because the double-invoke
    // produced two identical cards. `addToast` now COLLAPSES an identical toast
    // (ten rapid clicks otherwise stacked ten of them off the top of the
    // viewport), so the double-invoke yields ONE card — and a length of 1 is
    // what a single invoke would give too, which would make the old partner
    // vacuous. The id is the witness instead: the counter reaching `toast-2`
    // can only happen if `addToast` was called twice.
    expect(seen.toasts).toHaveLength(1);
    expect(seen.toasts[0].id).toBe("toast-2");
    // Every toast on screen must own a live timer — that is the invariant the
    // arm-in-addToast version broke (it had 2 toasts and 1 timer, and with the
    // collapse it would now be 1 toast and 0 timers: still red, still for the
    // same reason).
    expect(vi.getTimerCount()).toBe(seen.toasts.length);
    expect(vi.getTimerCount()).toBe(1);

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(seen.toasts).toHaveLength(0);
  });

  // RED if the arming effect stops pruning: the map keeps the timer for a toast
  // that is already gone, and the count stays 1.
  it("drops the timer for a toast dismissed by hand", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ kind: "info", title: "Saved", message: "All good." });
    });
    expect(vi.getTimerCount()).toBe(1);

    act(() => {
      result.current.removeToast(result.current.toasts[0].id);
    });

    expect(result.current.toasts).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});

/**
 * Stacking limits. `ToastHost` is a `position: fixed` column with no scroll, so
 * a card pushed past the top of the viewport can be neither read nor
 * dismissed. Measured in a real browser before these: ten rapid clicks on one
 * landing question produced ten byte-identical cards, a 1,385 px column in a
 * 900 px viewport, FOUR of them off the top of the screen.
 */
describe("useToast stacking limits", () => {
  // RED if the dedupe filter in addToast is removed: length becomes 5.
  it("collapses an identical toast instead of stacking it, and re-arms its timer", () => {
    const { result } = renderHook(() => useToast());
    for (let i = 0; i < 5; i++) {
      act(() => result.current.addToast({ kind: "info", title: "T", message: "M" }));
    }
    expect(result.current.toasts).toHaveLength(1);
    // A NEW id each time, which is what re-arms the 5 s dismissal — a repeated
    // action must not inherit the first one's nearly-expired timer.
    expect(result.current.toasts[0].id).toBe("toast-5");
  });

  // RED if `.slice(-MAX_VISIBLE_TOASTS)` is removed: length becomes 6.
  // This is the partner the dedupe test cannot be: identical toasts collapse to
  // one and never reach the cap, so without DISTINCT toasts the cap is untested.
  it("caps DISTINCT toasts, keeping the newest", () => {
    const { result } = renderHook(() => useToast());
    for (let i = 0; i < 6; i++) {
      act(() => result.current.addToast({ kind: "info", title: `T${i}`, message: `M${i}` }));
    }
    expect(result.current.toasts).toHaveLength(3);
    expect(result.current.toasts.map((t) => t.title)).toEqual(["T3", "T4", "T5"]);
  });

  // NOTE, deliberately not a bite-line: this test does NOT distinguish
  // `.slice(-N)` from `.slice(0, N)`. With two toasts and a cap of three the
  // two are the same array, so the claim originally written here ("RED if the
  // cap drops the newest") was FALSE. That mutant is killed by the
  // "caps DISTINCT toasts, keeping the newest" test above, which has six. This
  // one covers something else and is kept for it: that a list UNDER the cap is
  // returned untouched, i.e. the cap is a ceiling and not a floor.
  it("keeps fewer than the cap untouched, so the cap is not a floor", () => {
    const { result } = renderHook(() => useToast());
    act(() => result.current.addToast({ kind: "error", title: "A", message: "a" }));
    act(() => result.current.addToast({ kind: "error", title: "B", message: "b" }));
    expect(result.current.toasts.map((t) => t.title)).toEqual(["A", "B"]);
  });
});
