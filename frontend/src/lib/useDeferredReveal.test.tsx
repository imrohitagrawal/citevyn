/**
 * useDeferredReveal (#358).
 *
 * The behaviour that matters most here is the FAIL-OPEN one: with no
 * IntersectionObserver the hook must start revealed. Get that wrong and the
 * whole marketing page silently disappears for every reader whose browser
 * lacks it — and, closer to home, for every jsdom unit test that renders
 * LandingPage.
 */
import { createRef } from "react";
import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useDeferredReveal } from "./useDeferredReveal";

type ObserverCallback = (entries: Array<{ isIntersecting: boolean }>) => void;

/** A recording stand-in for the IntersectionObserver jsdom does not implement. */
function installObserver() {
  const instances: Array<{
    callback: ObserverCallback;
    options: IntersectionObserverInit | undefined;
    observed: Element[];
    disconnected: number;
  }> = [];

  class FakeIntersectionObserver {
    constructor(callback: ObserverCallback, options?: IntersectionObserverInit) {
      this.record = { callback, options, observed: [], disconnected: 0 };
      instances.push(this.record);
    }
    private record: (typeof instances)[number];
    observe(el: Element) {
      this.record.observed.push(el);
    }
    disconnect() {
      this.record.disconnected += 1;
    }
    unobserve() {}
    takeRecords() {
      return [];
    }
  }

  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  return instances;
}

/** Renders the hook against a real DOM node and exposes its return value. */
function renderHarness() {
  const seen: Array<{ revealed: boolean; reveal: () => void }> = [];
  const ref = createRef<HTMLDivElement>();

  function Harness() {
    const value = useDeferredReveal(ref);
    seen.push(value);
    return <div ref={ref} data-testid="sentinel" />;
  }

  const utils = render(<Harness />);
  return { ...utils, ref, seen, last: () => seen[seen.length - 1] };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useDeferredReveal", () => {
  it("starts REVEALED when the environment has no IntersectionObserver", () => {
    vi.stubGlobal("IntersectionObserver", undefined);

    const { seen } = renderHarness();

    // Revealed on the FIRST render, not after an effect: a test that asserts
    // synchronously after render() must already see the content.
    expect(seen[0].revealed).toBe(true);
  });

  it("starts hidden and observes the sentinel when IntersectionObserver exists", () => {
    const instances = installObserver();

    const { seen, ref } = renderHarness();

    expect(seen[0].revealed).toBe(false);
    expect(instances).toHaveLength(1);
    expect(instances[0].observed).toEqual([ref.current]);
    // Fetches ahead of the fold rather than at it, so the chunk is usually
    // there before the reader arrives.
    expect(instances[0].options?.rootMargin).toBe("400px 0px");
  });

  it("reveals when the sentinel intersects, and stops observing", () => {
    const instances = installObserver();
    const { last } = renderHarness();

    act(() => instances[0].callback([{ isIntersecting: true }]));

    expect(last().revealed).toBe(true);
    expect(instances[0].disconnected).toBeGreaterThan(0);
  });

  it("ignores a non-intersecting entry", () => {
    const instances = installObserver();
    const { last } = renderHarness();

    act(() => instances[0].callback([{ isIntersecting: false }]));

    expect(last().revealed).toBe(false);

    // PARTNER: the same callback DOES reveal when the entry is intersecting,
    // so the assertion above is about the isIntersecting check and not about a
    // callback that never had any effect.
    act(() => instances[0].callback([{ isIntersecting: true }]));
    expect(last().revealed).toBe(true);
  });

  it("reveals on demand, for a nav click whose target is inside the strip", () => {
    installObserver();
    const { last } = renderHarness();

    expect(last().revealed).toBe(false);
    act(() => last().reveal());

    expect(last().revealed).toBe(true);
  });

  it("never un-reveals once revealed", () => {
    const instances = installObserver();
    const { last, seen } = renderHarness();

    act(() => last().reveal());
    act(() => instances[0].callback([{ isIntersecting: false }]));

    expect(last().revealed).toBe(true);
    expect(seen.every((s, i) => (i > 0 && seen[i - 1].revealed ? s.revealed : true))).toBe(true);
  });

  it("disconnects the observer when the component unmounts", () => {
    const instances = installObserver();
    const { unmount } = renderHarness();

    unmount();

    expect(instances[0].disconnected).toBeGreaterThan(0);
  });
});
