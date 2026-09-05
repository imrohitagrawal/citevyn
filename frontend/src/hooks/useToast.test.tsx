import { describe, it, expect, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useToast } from "./useToast";

/**
 * A toast schedules a 5 s auto-dismiss `setTimeout`. Nothing cancelled it, so a
 * tree unmounted inside that window left the callback queued to call
 * `setToasts` on a component that was already gone.
 *
 * The visible damage is in the test runner rather than the browser: the
 * callback fires after vitest has torn the environment down, and vitest exits
 * 1 while reporting every test as passed. That is `type-check + unit tests +
 * build` — a REQUIRED check — going red with a green-looking report. Observed
 * on unmodified `main` in 1 of 6 full-suite runs during the #344 work.
 *
 * RED without the unmount cleanup in useToast.ts: `vi.getTimerCount()` stays 1
 * after unmount instead of dropping to 0.
 */
describe("useToast cancels its auto-dismiss timers on unmount", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

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

  it("still auto-dismisses while the tree is alive", () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useToast());

    act(() => {
      result.current.addToast({ kind: "info", title: "Saved", message: "All good." });
    });
    expect(result.current.toasts).toHaveLength(1);

    // Proves the cleanup did not simply disable auto-dismiss: the toast must
    // still disappear on its own after 5 s.
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(result.current.toasts).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});
