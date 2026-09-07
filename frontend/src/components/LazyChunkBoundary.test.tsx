/**
 * LazyChunkBoundary (#358).
 *
 * The behaviour under test is containment: when a lazily-imported chunk fails
 * to arrive, the reader loses that chunk's content and NOTHING ELSE. Reproduced
 * in review before this boundary existed — 404ing the strip's chunk on the
 * production build and scrolling took the page to `bodyTextLen=0`, a white
 * screen, because React unmounts the whole root on an uncaught render error and
 * `<Suspense>` does not catch a REJECTED lazy import (only a pending one).
 */
import "@testing-library/jest-dom/vitest";
import { lazy, Suspense } from "react";
import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LazyChunkBoundary } from "./LazyChunkBoundary";

function Boom(): JSX.Element {
  throw new Error("Failed to fetch dynamically imported module: /assets/landing-strip-x.js");
}

let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  // React logs the caught error itself; silence both that and our own line so
  // an expected failure does not look like a broken test run.
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("LazyChunkBoundary", () => {
  it("renders its children when nothing goes wrong", () => {
    render(
      <LazyChunkBoundary label="landing-strip">
        <p>the strip</p>
      </LazyChunkBoundary>,
    );

    expect(screen.getByText("the strip")).toBeInTheDocument();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("keeps the REST of the page alive when the chunk throws", () => {
    render(
      <div>
        <header>eager header</header>
        <LazyChunkBoundary label="landing-strip">
          <Boom />
        </LazyChunkBoundary>
        <footer>eager footer</footer>
      </div>,
    );

    // The whole point: siblings survive. Without the boundary React unmounts
    // the entire root and this assertion is what goes red.
    expect(screen.getByText("eager header")).toBeInTheDocument();
    expect(screen.getByText("eager footer")).toBeInTheDocument();
  });

  it("renders the fallback in place of the failed subtree", () => {
    render(
      <LazyChunkBoundary label="landing-strip" fallback={<p>nothing to see</p>}>
        <Boom />
      </LazyChunkBoundary>,
    );

    expect(screen.getByText("nothing to see")).toBeInTheDocument();
  });

  it("does not swallow the failure — it logs it, naming the chunk", () => {
    render(
      <LazyChunkBoundary label="landing-strip">
        <Boom />
      </LazyChunkBoundary>,
    );

    // A chunk that never arrives is invisible in the DOM, so "the page is
    // short" would otherwise be the only symptom.
    const ours = errorSpy.mock.calls.filter((c) => String(c[0]).includes("[landing-strip]"));
    expect(ours.length).toBeGreaterThan(0);
    expect(String(ours[0][1])).toContain("Failed to fetch dynamically imported module");
  });

  it("catches a REJECTED lazy import, which Suspense does not", async () => {
    const Never = lazy(() => Promise.reject(new Error("chunk 404")));

    render(
      <div>
        <p>eager sibling</p>
        <LazyChunkBoundary label="landing-strip">
          <Suspense fallback={<p>loading</p>}>
            <Never />
          </Suspense>
        </LazyChunkBoundary>
      </div>,
    );

    // The rejection resolves asynchronously; the sibling must still be there
    // afterwards. This is the exact shape LandingPage ships.
    await vi.waitFor(() => expect(screen.queryByText("loading")).not.toBeInTheDocument());
    expect(screen.getByText("eager sibling")).toBeInTheDocument();
  });
});
