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
    expect(seen.toasts.length).toBe(2);
    // Every toast on screen must own a live timer — that is the invariant the
    // arm-in-addToast version broke (it had 2 toasts and 1 timer).
    expect(vi.getTimerCount()).toBe(seen.toasts.length);

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
