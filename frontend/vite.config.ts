/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { liveStubPlugin } from "./vite.liveStub";

// Vite config for the CiteVyn web UI.
//
// The dev server binds to port 3000 — this matches the default
// CORS allowlist baked into the backend
// (DEFAULT_CORS_ALLOWED_ORIGINS in app/core/config.py). If you
// change the port here, also set
// ``CITEVYN_CORS_ALLOWED_ORIGINS`` in the backend's .env so the
// browser is allowed to talk to it.
//
// Vite proxies ``/v1`` and ``/health`` to the FastAPI server so
// the dev experience is "single origin" — no CORS preflight in
// the browser, and the production bundle just points at the
// real API host via ``VITE_API_BASE_URL``.
//
// LIVE-STUB MODE: when ``VITE_LIVE_STUB=1`` is set (used by
// ``playwright.live.config.ts``), the in-process stub plugin
// handles ``/v1/...`` and ``/health`` directly, so the dev
// server doesn't need a backend running. Set the proxy to
// ``undefined`` in that mode so it doesn't try to forward to a
// port nothing is listening on (which would surface as an
// ECONNREFUSED in the browser and a confusing test failure).
export default defineConfig({
  plugins: [react(), liveStubPlugin()],
  server: {
    port: 3000,
    strictPort: true,
    watch: {
      // Playwright (which reuses this dev server via reuseExistingServer)
      // writes screenshots/videos/reports into these dirs *during* a run.
      // If the dev server watches them, those writes fire an HMR page reload
      // that destroys the in-flight test's execution context — a
      // nondeterministic flake that only shows up under load. Ignoring the
      // test-output dirs makes `npm run test:ui` deterministic at retries:0,
      // instead of masking the reload with retries.
      ignored: [
        "**/test-results/**",
        "**/playwright-report/**",
        "**/.playwright-artifacts-*/**",
      ],
    },
    proxy:
      process.env.VITE_LIVE_STUB === "1"
        ? undefined
        : {
            "/v1": {
              target: "http://127.0.0.1:8000",
              changeOrigin: true,
            },
            "/health": {
              target: "http://127.0.0.1:8000",
              changeOrigin: true,
            },
          },
  },
  preview: {
    port: 4173,
    strictPort: true,
  },
  // Vitest unit-test config. Playwright (e2e/) is separate; unit tests
  // live next to the code they cover as ``*.test.ts(x)`` and run in a
  // jsdom environment so the React hooks can be exercised headlessly.
  // ``globals: false`` — tests import ``describe``/``it``/``vi`` from
  // ``vitest`` explicitly so tsc's ``noUnusedLocals`` stays happy and
  // no ambient global types leak into the app build.
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    exclude: ["e2e/**", "node_modules/**"],
  },
});
