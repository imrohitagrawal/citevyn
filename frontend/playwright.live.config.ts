import { defineConfig, devices } from "@playwright/test";

/**
 * Live-mode Playwright config.
 *
 * Companion to ``playwright.config.ts`` (demo mode). Enables the
 * in-process backend stub via ``VITE_LIVE_STUB=1``, flips the front
 * end to live mode via ``VITE_API_LIVE=true``, and uses
 * ``grep: "live only"`` so only tests that exercise the
 * ``sendLive`` / ``state.pending`` path run.
 *
 * Run locally:
 *   VITE_LIVE_STUB=1 npx playwright test --config=playwright.live.config.ts
 *
 * In CI, the ``frontend-e2e-live`` workflow runs this config against
 * a fresh dev server (no backend needed). The demo Playwright job
 * remains the default merge gate.
 */
export default defineConfig({
  testDir: "./tests",
  timeout: 60000,
  expect: {
    timeout: 15000,
  },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  // No retries on live-mode failures — they almost always point at a
  // real regression in the pending/streaming wiring and a green-by-flake
  // is worse than a red signal.
  retries: 0,
  workers: 1,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report-live", open: "never" }],
  ],
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    storageState: undefined,
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    // Must match playwright.config.ts: never adopt a server whose env we cannot
    // inspect. This config is the one CI actually runs, and the failure mode
    // here is the worst kind -- a stray demo-mode server makes the live tests
    // read `DEMO` from the badge and `test.skip` themselves into a SILENT GREEN.
    reuseExistingServer: false,
    timeout: 120000,
    stdout: "pipe",
    stderr: "pipe",
    env: {
      // Drives the stub (see vite.liveStub.ts) AND flips the front end
      // into the live path so ``state.pending`` goes true. Without both,
      // the loading-indicator bubble never appears.
      VITE_LIVE_STUB: "1",
      VITE_API_LIVE: "true",
    },
  },
  // Restrict to live-only tests so this config doesn't double-run
  // the demo-path suite. The string "(live only)" is in the test
  // titles for the only-live tests (see behavior.spec.ts). Playwright
  // requires a RegExp here (not a string).
  grep: /live only/i,
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
