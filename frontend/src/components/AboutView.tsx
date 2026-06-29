/**
 * About view — service health + a "what is this" + a curl example.
 *
 * Three jobs:
 *   1. Hit ``GET /health`` on mount and render the status, so a
 *      reviewer can verify the backend is reachable in one click.
 *   2. Explain what CiteVyn is, in two paragraphs, written for
 *      a non-engineer.
 *   3. Show a copy-pasteable curl example for the chat endpoint
 *      so the API is one ``pbcopy`` away from the terminal.
 */

import { useEffect, useState } from "react";

import { getHealth } from "../lib/api";
import type { HealthResponse } from "../lib/types";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "") || "http://127.0.0.1:8000";
const DEMO_TOKEN = import.meta.env.VITE_API_DEMO_KEY ?? "local-demo-key";

const CURL_EXAMPLE = `# Create a session
curl -sS -X POST ${API_BASE_URL}/v1/sessions \\
  -H "Authorization: Bearer ${DEMO_TOKEN}" \\
  -H "Content-Type: application/json" \\
  -d '{"user_id": "demo_user", "channel": "chat"}'

# Ask a question (use the session_id from above)
curl -sS -X POST ${API_BASE_URL}/v1/sessions/<SESSION_ID>/messages \\
  -H "Authorization: Bearer ${DEMO_TOKEN}" \\
  -H "Content-Type: application/json" \\
  -d '{"message": "What is the default rate limit for the Claude API?", "answer_style": "short"}'

# Look up an exact term
curl -sS -X POST ${API_BASE_URL}/v1/search/exact \\
  -H "Authorization: Bearer ${DEMO_TOKEN}" \\
  -H "Content-Type: application/json" \\
  -d '{"term": "CLAUDE_API_RATE_LIMIT", "product_area": "claude_api"}'`;

export function AboutView() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const h = await getHealth();
        if (!cancelled) setHealth(h);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const onCopyCurl = async () => {
    try {
      await navigator.clipboard.writeText(CURL_EXAMPLE);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard may be denied in an iframe; fail silently.
    }
  };

  return (
    <section className="well well--narrow view" aria-label="About CiteVyn">
      <header className="well__header">
        <h1 className="well__title">About CiteVyn</h1>
        <p className="well__subtitle">
          A cited RAG assistant for AI developer tools. Every answer is
          grounded in indexed official documentation and links back to
          the source.
        </p>
      </header>

      <div className="about-grid">
        <article className="card">
          <h2 className="card__title">Service health</h2>
          <p className="card__subtitle">
            Live check against <code className="mono">/health</code>.
          </p>
          <HealthStatus health={health} error={error} />
        </article>

        <article className="card">
          <h2 className="card__title">What it does</h2>
          <p className="card__title" style={{ fontSize: "var(--text-sm)", fontWeight: "var(--weight-regular)", color: "var(--text-secondary)" }}>
            Ask a question in plain English. CiteVyn classifies the
            domain, routes to the right retrieval path, generates a
            grounded answer, and validates every claim against the
            indexed source. If the source doesn't support the answer,
            it says so — explicitly.
          </p>
        </article>
      </div>

      <article className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 className="card__title">Try it from your terminal</h2>
          <button
            type="button"
            className="button button--secondary button--small"
            onClick={onCopyCurl}
            data-testid="copy-curl-btn"
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <p className="card__subtitle">
          The same calls the UI makes, so you can wire CiteVyn into
          your own scripts.
        </p>
        <pre className="code-block" aria-label="curl example">
{CURL_EXAMPLE}
        </pre>
      </article>

      <article className="card">
        <h2 className="card__title">API surface</h2>
        <p className="card__subtitle">The full contract is in <code className="mono">docs/API_SPEC.md</code>.</p>
        <ul className="muted small" style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)", lineHeight: "var(--leading-loose)", listStyle: "none" }}>
          <li><code className="mono">POST /v1/sessions</code> — create a session</li>
          <li><code className="mono">POST /v1/sessions/&#123;id&#125;/messages</code> — ask a question</li>
          <li><code className="mono">POST /v1/search/exact</code> — exact-term lookup</li>
          <li><code className="mono">POST /v1/feedback</code> — V1 placeholder</li>
          <li><code className="mono">GET /health</code> — service health</li>
        </ul>
      </article>
    </section>
  );
}

function HealthStatus({ health, error }: { health: HealthResponse | null; error: string | null }) {
  if (error) {
    return (
      <div className="about-health">
        <span className="about-health__indicator about-health__indicator--unhealthy" aria-hidden="true" />
        <div className="column" style={{ gap: "var(--space-1)" }}>
          <span className="about-health__status">Backend unreachable</span>
          <span className="about-health__detail">{error}</span>
        </div>
      </div>
    );
  }
  if (!health) {
    return (
      <div className="about-health">
        <span className="about-health__indicator about-health__indicator--degraded" aria-hidden="true" />
        <div className="column" style={{ gap: "var(--space-1)" }}>
          <span className="about-health__status">Checking…</span>
          <span className="about-health__detail">Pinging <code className="mono">/health</code></span>
        </div>
      </div>
    );
  }
  const cls =
    health.status === "healthy"
      ? "about-health__indicator--healthy"
      : health.status === "degraded"
        ? "about-health__indicator--degraded"
        : "about-health__indicator--unhealthy";
  return (
    <div className="about-health">
      <span className={`about-health__indicator ${cls}`} aria-hidden="true" />
      <div className="column" style={{ gap: "var(--space-1)" }}>
        <span className="about-health__status">{health.status}</span>
        {health.detail && <span className="about-health__detail">{health.detail}</span>}
        {health.components && (
          <ul className="about-health__detail" style={{ listStyle: "none", marginTop: "var(--space-1)" }}>
            {Object.entries(health.components).map(([k, v]) => (
              <li key={k}>
                <span className="tiny muted">{k}:</span> <code className="mono tiny">{v}</code>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
