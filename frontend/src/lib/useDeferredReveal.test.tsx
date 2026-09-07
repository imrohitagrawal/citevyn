/**
 * useDeferredReveal (#358).
 *
 * Two behaviours here matter more than the rest:
 *
 *   FAIL-OPEN — with no IntersectionObserver the hook must start revealed. Get
 *   that wrong and the whole marketing page silently disappears for every
 *   reader whose browser lacks it, and for every jsdom unit test that renders
 *   LandingPage.
 *
 *   FOLLOWING THE NODE — the sentinel is destroyed and recreated whenever
 *   LandingPage swaps between the landing and chat screens. The first version
 *   of this hook attached the observer in a useEffect over a RefObject and
 *   silently kept watching the DETACHED original node, so coming back from the
 *   chat left the strip unreachable by scrolling. "reattaches after a remount"
 *   below is the regression test for that.
 */
import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useDeferredReveal } from "./useDeferredReveal";

type ObserverCallback = (entries: Array<{ isIntersecting: boolean }>) => void;

interface Recorded {
  callback: ObserverCallback;
  options: IntersectionObserverInit | undefined;
  observed: Element[];
  disconnected: number;
}

/** A recording stand-in for the IntersectionObserver jsdom does not implement. */
function installObserver() {
  const instances: Recorded[] = [];

  class FakeIntersectionObserver {
    private record: Recorded;
    constructor(callback: ObserverCallback, options?: IntersectionObserverInit) {
      this.record = { callback, options, observed: [], disconnected: 0 };
      instances.push(this.record);
    }
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

type Hook = ReturnType<typeof useDeferredReveal>;

/**
 * Renders the hook against a real DOM node. `showSentinel` mirrors what
 * LandingPage does when it swaps <main> out for the chat screen.
 */
function renderHarness(showSentinel = true) {
  const seen: Hook[] = [];

  function Harness({ show }: { show: boolean }) {
    const value = useDeferredReveal();
    seen.push(value);
    return show ? <div ref={value.sentinelRef} data-testid="sentinel" /> : <p>chat</p>;
  }

  const utils = render(<Harness show={showSentinel} />);
  return {
    ...utils,
    seen,
    last: () => seen[seen.length - 1],
    sentinel: () => utils.container.querySelector('[data-testid="sentinel"]'),
    setShow: (show: boolean) => act(() => utils.rerender(<Harness show={show} />)),
  };
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

    const { seen, sentinel } = renderHarness();

    expect(seen[0].revealed).toBe(false);
    expect(instances).toHaveLength(1);
    expect(instances[0].observed).toEqual([sentinel()]);
    // Fetches ahead of the fold rather than at it, so the chunk is usually
    // there before the reader arrives.
    expect(instances[0].options?.rootMargin).toBe("400px 0px");
  });

  it("reveals when the sentinel intersects, and stops observing from the callback itself", () => {
    const instances = installObserver();
    const { last } = renderHarness();

    // The disconnect is sampled INSIDE the act body, right after the callback
    // runs and before React flushes the state update. Sampling it afterwards
    // proves nothing about the `observer.disconnect()` in the callback: once
    // `revealed` flips, the ref callback's identity changes and its teardown
    // disconnects too, so a second mechanism supplies the same observation.
    // Mutation-tested — with an after-the-fact assertion, deleting EITHER
    // disconnect alone left this test green.
    let disconnectedInCallback = -1;
    act(() => {
      instances[0].callback([{ isIntersecting: true }]);
      disconnectedInCallback = instances[0].disconnected;
    });

    expect(disconnectedInCallback).toBe(1);
    expect(last().revealed).toBe(true);
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
    const { last } = renderHarness();

    act(() => last().reveal());
    act(() => instances[0].callback([{ isIntersecting: false }]));

    expect(last().revealed).toBe(true);
  });

  it("reattaches to a NEW sentinel after the node is unmounted and remounted", () => {
    // LandingPage destroys <main> (and the sentinel with it) for the chat
    // screen, then rebuilds it on the way back. Watching the old detached node
    // means the reader can never scroll the strip into existence again.
    const instances = installObserver();
    const { last, sentinel, setShow } = renderHarness();

    const first = sentinel();
    expect(instances).toHaveLength(1);
    expect(instances[0].observed).toEqual([first]);

    setShow(false); // -> chat screen
    expect(instances[0].disconnected).toBeGreaterThan(0);

    setShow(true); // -> back to landing
    const second = sentinel();
    expect(second).not.toBeNull();
    expect(second).not.toBe(first);

    // A SECOND observer, watching the NEW node.
    expect(instances).toHaveLength(2);
    expect(instances[1].observed).toEqual([second]);

    // And it actually works: firing the new observer reveals.
    expect(last().revealed).toBe(false);
    act(() => instances[1].callback([{ isIntersecting: true }]));
    expect(last().revealed).toBe(true);
  });

  it("does not observe a remounted sentinel once already revealed", () => {
    const instances = installObserver();
    const { last, setShow } = renderHarness();

    act(() => last().reveal());
    const afterReveal = instances.length;

    setShow(false);
    setShow(true);

    // Nothing left to watch for — re-observing would be wasted work and could
    // re-fire setState on a component that has already committed.
    expect(instances).toHaveLength(afterReveal);
    expect(last().revealed).toBe(true);
  });

  it("fails OPEN when the IntersectionObserver constructor THROWS", () => {
    // A malformed rootMargin raises SyntaxError from the constructor (verified
    // in Chromium: "400" and "400 px 0px" both throw). Guarding only for the
    // API being undefined would let that escape a ref callback and take the
    // page down — the opposite of this hook's stated invariant.
    class Exploding {
      constructor() {
        throw new SyntaxError("Failed to construct 'IntersectionObserver': rootMargin");
      }
    }
    vi.stubGlobal("IntersectionObserver", Exploding);

    const { last, seen } = renderHarness();

    // Not thrown out of render, and the content is SHOWN rather than hidden.
    expect(seen.length).toBeGreaterThan(0);
    expect(last().revealed).toBe(true);
  });

  it("disconnects the observer when the component unmounts", () => {
    const instances = installObserver();
    const { unmount } = renderHarness();

    unmount();

    expect(instances[0].disconnected).toBeGreaterThan(0);
  });
});
