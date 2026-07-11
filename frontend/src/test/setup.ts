/**
 * Vitest global setup.
 *
 * Unmounts any React trees rendered by ``@testing-library/react``
 * after each test so hook state, timers, and DOM nodes never leak
 * across cases. Kept intentionally tiny — matcher extensions are
 * imported per-test where needed rather than globally.
 */
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
});
