import base from "/Users/rohitagrawal/Projects/citevyn/frontend/playwright.demo-ci.config";
import { defineConfig } from "@playwright/test";
export default defineConfig({ ...base, workers: 1,
  use: { ...base.use, baseURL: "http://localhost:3112" },
  webServer: { command: "npx vite --port 3112 --strictPort", url: "http://localhost:3112",
    reuseExistingServer: false, timeout: 120000, env: { VITE_API_LIVE: "false" } } });
