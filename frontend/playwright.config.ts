import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 60000,
  expect: {
    timeout: 15000,
  },
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    // Clear storage before each test for clean state
    storageState: undefined,
  },
  // Spin up Vite dev server automatically before running tests.
  // Force demo mode by default — chat interaction tests don't depend on
  // the backend (which is rate-limited and slow) and the demo path
  // exercises the same streamText/emitter code as the live path.
  // Tests that DO need the live backend opt in via the ``live`` param.
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    // Do NOT reuse. Playwright cannot inspect the env of a server it adopts, so
    // a leftover `npm run dev` (started from ``.env.local``, which sets
    // VITE_API_LIVE=true) silently defeats the ``VITE_API_LIVE: "false"``
    // override below and the entire chat suite fails against a backend that
    // isn't running. That is precisely what happened here: a 19-hour-old dev
    // server turned a green suite into 13 phantom failures that read as an app
    // regression. With reuse off, a stray server causes a LOUD port conflict
    // instead of a quietly wrong result — the failure mode you can debug.
    // (The conventional `!process.env.CI` does not help: the contamination is a
    // LOCAL problem, and that setting keeps reuse switched on locally.)
    reuseExistingServer: false,
    timeout: 120000,
    stdout: "pipe",
    stderr: "pipe",
    env: {
      // Override .env.local so Vite starts in demo mode. Live-mode tests
      // skip if the backend isn't wired up.
      VITE_API_LIVE: "false",
    },
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
