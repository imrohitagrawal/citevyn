import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

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
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
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
});
