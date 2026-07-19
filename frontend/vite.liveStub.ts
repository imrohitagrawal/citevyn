/**
 * Vite plugin that stubs the backend in-process so the live-mode
 * Playwright suite can exercise the ``sendLive`` path without
 * needing a real FastAPI server running.
 *
 * Mounts at the following routes when ``VITE_LIVE_STUB=1`` is set in
 * the process env (this is the only switch — there is no config
 * object so the surface stays minimal):
 *
 * - ``POST /v1/sessions`` returns ``{ session_id: "stub-session" }``
 *   after 50ms.
 * - ``POST /v1/sessions/:id/messages`` returns
 *   ``{ answer: "<canned answer>", citations: [...], unsupported: false,
 *      no_answer: false }`` after a per-test controllable delay
 *   (default 800ms — long enough to observe the pending indicator).
 *
 * The plugin is intentionally tiny: the live test only needs to
 * prove the front-end pipeline (pending bubble → /v1/messages →
 * streamBot → bubble renders; dedup guard → flashExisting →
 * scroll/pulse). Anything else would be testing real backend
 * behaviour, which belongs in ``backend/tests`` not here.
 */
import type { Plugin } from "vite";
import type { ServerResponse } from "node:http";

const STUB_DELAY_MS = 800;

const CANNED_ANSWER =
  "Claude Code is Anthropic's agentic coding tool. It runs in your terminal, as a " +
  "desktop or web app, or as an IDE extension; it reads your repo and applies edits " +
  "through permissioned tools. Use it for multi-file refactors, test runs, and PR " +
  "preparation.";

const CANNED_CITATIONS = [
  {
    citation_id: "stub-1",
    source_id: "stub-doc-1",
    title: "Claude Code overview",
    snippet: "Anthropic's CLI for coding tasks.",
    product_area: "claude_code",
  },
];

function send(res: ServerResponse, status: number, body: unknown): void {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(body));
}

export function liveStubPlugin(): Plugin {
  return {
    name: "citevyn-live-stub",
    apply: () => process.env.VITE_LIVE_STUB === "1",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url) return next();
        if (req.method !== "POST") return next();
        // Strip any query string before matching — vite passes the raw
        // URL (including ?foo=bar) to middleware, so an anchored
        // pattern would silently miss and fall through to the SPA
        // fallback instead of returning a 200.
        const path = req.url.split("?", 1)[0];

        if (path === "/v1/sessions") {
          await new Promise((r) => setTimeout(r, 50));
          send(res, 200, { session_id: "stub-session" });
          return;
        }

        const msgMatch = /^\/v1\/sessions\/([^/]+)\/messages\/?$/.exec(path);
        if (msgMatch) {
          // Per-test override: ``X-Stub-Delay-Ms`` lets the test nudge the
          // delay without restarting the dev server.
          const override = Number(req.headers["x-stub-delay-ms"]);
          const delay =
            Number.isFinite(override) && override >= 0 ? override : STUB_DELAY_MS;
          await new Promise((r) => setTimeout(r, delay));
          send(res, 200, {
            answer: CANNED_ANSWER,
            citations: CANNED_CITATIONS,
            unsupported: false,
            no_answer: false,
          });
          return;
        }

        next();
      });
    },
  };
}
