/**
 * Landing framing — the brutalist-lite sections.
 *
 * Lives around the chat surface (see App.tsx). All sections
 * below the hero scroll past the active chat panel; the hero
 * itself is the landing header the user lands on.
 *
 * Sections, in scroll order:
 *   1. Hero — Anton display headline + yellow highlight bar +
 *      side-by-side waitlist form.
 *   2. Social proof — single trust strip.
 *   3. Problem vs Solution — full-bleed split, charcoal vs
 *      #272727.
 *   4. Bento feature grid — 3-col, 400px rows.
 *   5. Abstract UI mockup — browser-frame showcase.
 *   6. How it works — 1:2 split, sticky Anton title.
 *   7. Testimonials — alternating light/dark cards.
 *   8. Final CTA — high-energy yellow block.
 */

import { useState, type FormEvent } from "react";

import type { SessionId } from "../lib/types";

interface LandingViewProps {
  sessionId: SessionId | null;
  messageCount: number;
  indexVersion: string | null;
  answerPolicyVersion: string | null;
  onLaunchChat: (initialPrompt?: string) => void;
}

export function LandingView({
  sessionId,
  messageCount,
  indexVersion,
  answerPolicyVersion,
  onLaunchChat,
}: LandingViewProps) {
  return (
    <div className="landing" aria-label="Product overview">
      <Hero onLaunchChat={onLaunchChat} />
      <SocialProof />
      <ProblemSolution />
      <BentoFeatures onLaunchChat={onLaunchChat} />
      <UIMockup />
      <HowItWorks />
      <Testimonials />
      <FinalCta />
      <SessionFooter
        sessionId={sessionId}
        messageCount={messageCount}
        indexVersion={indexVersion}
        answerPolicyVersion={answerPolicyVersion}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Hero — Anton display + yellow highlight bar + waitlist form
// ---------------------------------------------------------------------------

function Hero({ onLaunchChat }: { onLaunchChat: (initialPrompt?: string) => void }) {
  const [email, setEmail] = useState("");

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = email.trim();
    if (!trimmed) return;
    onLaunchChat(`Hi CiteVyn — I'd like to know: ${trimmed.split("@")[0] || "everything"}.`);
    setEmail("");
  };

  return (
    <section className="landing__section landing__hero" id="hero">
      <div className="landing__hero-grid" aria-hidden="true" />
      <div className="landing__hero-inner">
        <span className="landing__badge">
          <span className="landing__badge-dot" aria-hidden="true" />
          <span className="landing__badge-text">JOIN THE WAITLIST · V1 IN PRIVATE BETA</span>
        </span>

        <h1 className="landing__hero-headline">
          Cited answers for
          <br />
          <span className="landing__highlight">
            <span className="landing__highlight-bar" aria-hidden="true" />
            <span className="landing__highlight-text">AI dev tools.</span>
          </span>
        </h1>

        <p className="landing__hero-subhead">
          Ask anything about Claude, Claude Code, Codex, or Gemini. CiteVyn routes
          your question to the right doc, generates a grounded answer, and links
          every claim back to the source. No hallucinated flags. No invented
          env vars.
        </p>

        <form className="landing__waitlist" onSubmit={onSubmit} aria-label="Join the waitlist">
          <input
            type="email"
            className="landing__waitlist-input"
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            aria-label="Email address"
            data-testid="waitlist-email"
          />
          <button type="submit" className="landing__waitlist-btn" data-testid="waitlist-submit">
            Try It
          </button>
        </form>

        <ul className="landing__hero-meta" aria-label="Stats">
          <li>
            <span className="landing__hero-meta-num">12k+</span>
            <span className="landing__hero-meta-label">indexed pages</span>
          </li>
          <li>
            <span className="landing__hero-meta-num">4</span>
            <span className="landing__hero-meta-label">AI surfaces covered</span>
          </li>
          <li>
            <span className="landing__hero-meta-num">100%</span>
            <span className="landing__hero-meta-label">citation coverage</span>
          </li>
        </ul>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Social proof strip
// ---------------------------------------------------------------------------

function SocialProof() {
  return (
    <section className="landing__section landing__proof">
      <p className="landing__proof-eyebrow">TRUSTED BY TEAMS BUILDING ON CLAUDE, CODEX, AND GEMINI</p>
      <div className="landing__proof-row">
        {["Anthropic", "OpenAI", "Google", "Cursor", "Replit", "Linear"].map((name) => (
          <span key={name} className="landing__proof-mark">
            {name}
          </span>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Problem vs Solution — full-bleed split
// ---------------------------------------------------------------------------

function ProblemSolution() {
  return (
    <section className="landing__section landing__contrast" id="problem-solution">
      <div className="landing__contrast-half landing__contrast-half--problem">
        <div className="landing__contrast-inner">
          <span className="landing__contrast-eyebrow">THE OLD WAY</span>
          <h2 className="landing__contrast-title">Pasting docs into ChatGPT and hoping.</h2>
          <ul className="landing__contrast-list">
            <li>
              <span className="landing__contrast-x" aria-hidden="true">✕</span>
              <span>Model invents flags that don't exist.</span>
            </li>
            <li>
              <span className="landing__contrast-x" aria-hidden="true">✕</span>
              <span>No way to verify a claim against the source.</span>
            </li>
            <li>
              <span className="landing__contrast-x" aria-hidden="true">✕</span>
              <span>Stale knowledge from 2023.</span>
            </li>
            <li>
              <span className="landing__contrast-x" aria-hidden="true">✕</span>
              <span>No exact-term lookup for env vars and config keys.</span>
            </li>
          </ul>
        </div>
      </div>
      <div className="landing__contrast-half landing__contrast-half--solution">
        <div className="landing__contrast-inner">
          <span className="landing__contrast-eyebrow landing__contrast-eyebrow--yellow">
            THE CITEVYN WAY
          </span>
          <h2 className="landing__contrast-title">
            Indexed source. <br /> Cited answers. <br /> Real docs.
          </h2>
          <ul className="landing__contrast-list">
            <li>
              <span className="landing__contrast-check" aria-hidden="true">
                ✓
              </span>
              <span>Every claim is grounded in the official docs.</span>
            </li>
            <li>
              <span className="landing__contrast-check" aria-hidden="true">
                ✓
              </span>
              <span>Click any citation to open the exact source page.</span>
            </li>
            <li>
              <span className="landing__contrast-check" aria-hidden="true">
                ✓
              </span>
              <span>Index refreshes on every official doc release.</span>
            </li>
            <li>
              <span className="landing__contrast-check" aria-hidden="true">
                ✓
              </span>
              <span>Exact lookup: flags, env vars, model IDs — no LLM.</span>
            </li>
          </ul>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Bento feature grid — 3 columns, 400px rows
// ---------------------------------------------------------------------------

function BentoFeatures({ onLaunchChat }: { onLaunchChat: (initialPrompt?: string) => void }) {
  return (
    <section className="landing__section landing__bento" id="features">
      <header className="landing__section-head">
        <span className="landing__section-eyebrow">FEATURES</span>
        <h2 className="landing__section-title">
          Everything you need to <span className="landing__highlight landing__highlight--inline">
            <span className="landing__highlight-bar" aria-hidden="true" />
            <span className="landing__highlight-text">trust</span>
          </span> an answer.
        </h2>
      </header>

      <div className="landing__bento-grid">
        <article className="landing__bento-card landing__bento-card--wide landing__bento-card--dark">
          <header className="landing__bento-card-head">
            <h3 className="landing__bento-card-title">Cited answers, every time.</h3>
            <p className="landing__bento-card-sub">
              We re-rank against your question, then attach a numbered marker
              to every claim. Click to open the source — no scrolling required.
            </p>
          </header>
          <div className="landing__bento-card-mockup" aria-hidden="true">
            <div className="landing__bento-card-mockup-row">
              <span className="landing__bubble landing__bubble--answer">
                The default Claude API rate limit is <strong>30 req/min</strong>
                <span className="landing__bubble-cite">[1]</span>.
              </span>
            </div>
            <div className="landing__bento-card-mockup-row">
              <span className="landing__bubble landing__bubble--cite">
                <span className="landing__bubble-cite-dot">1</span>
                <span>anthropic.com/docs/rate-limits</span>
              </span>
            </div>
          </div>
        </article>

        <article className="landing__bento-card">
          <header className="landing__bento-card-head">
            <h3 className="landing__bento-card-title">Exact search.</h3>
            <p className="landing__bento-card-sub">
              Paste a flag, env var, or model name. We look it up directly —
              no LLM in the loop.
            </p>
          </header>
          <div className="landing__bento-card-code" aria-hidden="true">
            <code className="mono small">$ citevyn exact --max-tokens</code>
            <div className="landing__bento-card-code-hits">
              <span className="landing__bento-card-code-hits-dot" />
              <span className="landing__bento-card-code-hits-dot" />
              <span className="landing__bento-card-code-hits-dot" />
              <span className="small">3 hits in claude_code</span>
            </div>
          </div>
        </article>

        <article className="landing__bento-card">
          <header className="landing__bento-card-head">
            <h3 className="landing__bento-card-title">Multi-product.</h3>
            <p className="landing__bento-card-sub">
              One search across Claude, Claude Code, Codex, and Gemini — we
              route to the right surface.
            </p>
          </header>
          <ul className="landing__bento-card-stack" aria-hidden="true">
            <li className="landing__bento-card-stack-item landing__bento-card-stack-item--claude">
              <span className="landing__bento-card-stack-dot" /> Claude API
            </li>
            <li className="landing__bento-card-stack-item landing__bento-card-stack-item--code">
              <span className="landing__bento-card-stack-dot" /> Claude Code
            </li>
            <li className="landing__bento-card-stack-item landing__bento-card-stack-item--codex">
              <span className="landing__bento-card-stack-dot" /> Codex
            </li>
            <li className="landing__bento-card-stack-item landing__bento-card-stack-item--gemini">
              <span className="landing__bento-card-stack-dot" /> Gemini
            </li>
          </ul>
        </article>

        <article className="landing__bento-card landing__bento-card--wide">
          <header className="landing__bento-card-head">
            <h3 className="landing__bento-card-title">Refuses to hallucinate.</h3>
            <p className="landing__bento-card-sub">
              If the indexed source doesn't support the answer, CiteVyn says
              so — explicitly. No hedging, no invented caveats.
            </p>
          </header>
          <div className="landing__bento-card-pulse" aria-hidden="true">
            <span className="landing__bento-card-pulse-dot" />
            <span className="landing__bento-card-pulse-dot" />
            <span className="landing__bento-card-pulse-dot" />
          </div>
        </article>

        <article className="landing__bento-card landing__bento-card--dark landing__bento-card--yellow-edge">
          <header className="landing__bento-card-head">
            <h3 className="landing__bento-card-title">Pulled from 4 product surfaces.</h3>
            <p className="landing__bento-card-sub">
              12,000+ pages indexed across Claude, Claude Code, Codex, Gemini.
              Updated on every release.
            </p>
          </header>
          <div className="landing__bento-card-stat-row">
            <div>
              <span className="landing__bento-card-stat-num">12k+</span>
              <span className="landing__bento-card-stat-label">pages</span>
            </div>
            <div>
              <span className="landing__bento-card-stat-num">4</span>
              <span className="landing__bento-card-stat-label">surfaces</span>
            </div>
            <div>
              <span className="landing__bento-card-stat-num">99.8%</span>
              <span className="landing__bento-card-stat-label">uptime</span>
            </div>
          </div>
        </article>
      </div>

      <div className="landing__bento-cta">
        <button type="button" className="button button--primary" onClick={() => onLaunchChat()} data-testid="bento-cta">
          Try a question →
        </button>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Abstract UI mockup — browser frame showcase
// ---------------------------------------------------------------------------

function UIMockup() {
  return (
    <section className="landing__section landing__mockup">
      <header className="landing__section-head landing__section-head--center">
        <span className="landing__section-eyebrow">SEE IT IN ACTION</span>
        <h2 className="landing__section-title">
          A workspace <span className="landing__highlight landing__highlight--inline">
            <span className="landing__highlight-bar" aria-hidden="true" />
            <span className="landing__highlight-text">built for citations.</span>
          </span>
        </h2>
      </header>

      <div className="landing__mockup-frame" aria-hidden="true">
        <div className="landing__mockup-titlebar">
          <span className="landing__mockup-dot landing__mockup-dot--red" />
          <span className="landing__mockup-dot landing__mockup-dot--yellow" />
          <span className="landing__mockup-dot landing__mockup-dot--green" />
          <span className="landing__mockup-title">citevyn.ai · ask</span>
        </div>

        <div className="landing__mockup-body">
          <aside className="landing__mockup-sidebar">
            <span className="tiny muted">PRODUCT</span>
            <ul className="landing__mockup-list">
              <li>Claude API</li>
              <li className="landing__mockup-list-active">Claude Code</li>
              <li>Codex</li>
              <li>Gemini</li>
            </ul>
            <span className="tiny muted">RECENT</span>
            <ul className="landing__mockup-list">
              <li>rate limit</li>
              <li>--max-tokens</li>
              <li>CLAUDE_API_KEY</li>
            </ul>
          </aside>

          <main className="landing__mockup-canvas">
            <div className="landing__mockup-canvas-card">
              <div className="landing__mockup-cursor">
                <span className="landing__mockup-cursor-name">Rohit A.</span>
              </div>
              <p className="landing__mockup-question">
                What is the default rate limit for the <strong>Claude API</strong>?
              </p>
              <p className="landing__mockup-answer">
                The default rate limit is <strong>30 req/min</strong> on the
                free tier, with a burst of <strong>60 req/min</strong>{" "}
                <span className="landing__bubble-cite landing__bubble-cite--dark">[1]</span>.
              </p>
            </div>
          </main>

          <aside className="landing__mockup-props">
            <div className="landing__mockup-props-row">
              <span className="tiny muted">DISPLAY</span>
              <span className="landing__mockup-props-val">Anton · 8xl</span>
            </div>
            <div className="landing__mockup-props-row">
              <span className="tiny muted">ALIGN</span>
              <span className="landing__mockup-props-icons" aria-hidden="true">
                <span>⫷</span><span>⫸</span><span>≡</span>
              </span>
            </div>
            <div className="landing__mockup-props-row">
              <span className="tiny muted">ACCENT</span>
              <span className="landing__mockup-swatch" />
            </div>
          </aside>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// How it works — 1:2 split with sticky Anton title
// ---------------------------------------------------------------------------

function HowItWorks() {
  const steps: ReadonlyArray<{ num: string; title: string; copy: string }> = [
    {
      num: "01",
      title: "Ask in plain English.",
      copy:
        "Type a question, paste a flag, or quote an error. CiteVyn parses the intent and the domain.",
    },
    {
      num: "02",
      title: "We route to the right doc.",
      copy:
        "Claude Code, Claude API, Codex, or Gemini — your question lands on the indexed source it actually lives in.",
    },
    {
      num: "03",
      title: "You get a cited answer.",
      copy:
        "A grounded response with numbered citations. Click any citation to open the source page.",
    },
  ];

  return (
    <section className="landing__section landing__how" id="how">
      <div className="landing__how-inner">
        <header className="landing__how-left">
          <span className="landing__section-eyebrow">HOW IT WORKS</span>
          <h2 className="landing__how-title">
            Three steps.
            <br />
            <span className="landing__highlight landing__highlight--inline">
              <span className="landing__highlight-bar" aria-hidden="true" />
              <span className="landing__highlight-text">Zero guesses.</span>
            </span>
          </h2>
        </header>

        <ol className="landing__how-steps">
          {steps.map((s) => (
            <li className="landing__how-step" key={s.num}>
              <span className="landing__how-num">{s.num}</span>
              <div className="landing__how-body">
                <h3 className="landing__how-step-title">{s.title}</h3>
                <p className="landing__how-step-copy">{s.copy}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Testimonials — alternating light/dark cards with oversized stars
// ---------------------------------------------------------------------------

function Testimonials() {
  const cards: ReadonlyArray<{
    body: string;
    name: string;
    role: string;
    variant: "light" | "dark";
  }> = [
    {
      body:
        "CiteVyn is the first AI tool that actually answers my CLI questions with a citation. I stopped copy-pasting docs into ChatGPT the day I tried it.",
      name: "PRIYA NAIDU",
      role: "Senior platform engineer · Linear",
      variant: "light",
    },
    {
      body:
        "I asked it for a flag that didn't exist. It told me — instead of inventing one. That alone is worth the waitlist.",
      name: "DANIEL ORTEGA",
      role: "Staff infra engineer · Replit",
      variant: "dark",
    },
    {
      body:
        "The exact-search view replaced three Notion docs and a Slack thread. Our onboarding is now ten minutes faster per engineer.",
      name: "KIM TANAKA",
      role: "Developer experience lead · Cursor",
      variant: "light",
    },
  ];

  return (
    <section className="landing__section landing__testimonials" id="testimonials">
      <header className="landing__section-head landing__section-head--center">
        <span className="landing__section-eyebrow">WHAT ENGINEERS SAY</span>
        <h2 className="landing__section-title">
          Built for <span className="landing__highlight landing__highlight--inline">
            <span className="landing__highlight-bar" aria-hidden="true" />
            <span className="landing__highlight-text">truth</span>
          </span>, not vibes.
        </h2>
      </header>

      <div className="landing__testimonials-grid">
        {cards.map((c) => (
          <article
            key={c.name}
            className={
              "landing__testimonial landing__testimonial--" + c.variant +
              (c.variant === "dark" ? " landing__testimonial--offset" : "")
            }
          >
            <div className="landing__testimonial-stars" aria-hidden="true">
              {[0, 1, 2, 3, 4].map((s) => (
                <Star key={s} />
              ))}
            </div>
            <p className="landing__testimonial-body">{c.body}</p>
            <footer className="landing__testimonial-foot">
              <span className="landing__testimonial-avatar" aria-hidden="true" />
              <div>
                <span className="landing__testimonial-name">{c.name}</span>
                <span className="landing__testimonial-role">{c.role}</span>
              </div>
            </footer>
          </article>
        ))}
      </div>
    </section>
  );
}

function Star() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" width="22" height="22">
      <path d="M12 2.6 14.94 8.55 21.6 9.54 16.8 14.21 17.88 20.84 12 17.74 6.12 20.84 7.2 14.21 2.4 9.54 9.06 8.55Z" />
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Final CTA — high-energy yellow block
// ---------------------------------------------------------------------------

function FinalCta() {
  const [email, setEmail] = useState("");

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (!email.trim()) return;
    setEmail("");
  };

  return (
    <section className="landing__section landing__final" id="cta">
      <div className="landing__final-overlay" aria-hidden="true">
        CITEVYN CITEVYN CITEVYN CITEVYN
      </div>
      <div className="landing__final-inner">
        <h2 className="landing__final-headline">
          Stop pasting docs.
          <br />
          <span className="landing__highlight landing__highlight--inline">
            <span className="landing__highlight-bar" aria-hidden="true" />
            <span className="landing__highlight-text">Start citing.</span>
          </span>
        </h2>
        <p className="landing__final-sub">
          Join 1,800+ engineers on the CiteVyn waitlist. V1 ships in private
          beta this quarter.
        </p>
        <form className="landing__final-form" onSubmit={onSubmit}>
          <input
            type="email"
            className="landing__final-input"
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            aria-label="Email address"
            data-testid="final-email"
          />
          <button type="submit" className="landing__final-btn" data-testid="final-submit">
            Get Early Access
          </button>
        </form>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Session footer — surfaces session id, message count, index/policy
// versions when a session exists. Hidden until the user has sent at
// least one message so first-load stays clean.
// ---------------------------------------------------------------------------

function SessionFooter({
  sessionId,
  messageCount,
  indexVersion,
  answerPolicyVersion,
}: {
  sessionId: SessionId | null;
  messageCount: number;
  indexVersion: string | null;
  answerPolicyVersion: string | null;
}) {
  if (!sessionId || messageCount === 0) {
    return null;
  }
  return (
    <section className="landing__section landing__session">
      <div className="landing__session-inner">
        <span className="landing__section-eyebrow">ACTIVE SESSION</span>
        <div className="landing__session-meta">
          <span>
            <span className="tiny muted">id</span> <code className="mono">{sessionId.slice(0, 8)}</code>
          </span>
          <span>
            <span className="tiny muted">messages</span>{" "}
            <strong>{messageCount}</strong>
          </span>
          <span>
            <span className="tiny muted">index</span>{" "}
            <code className="mono">{indexVersion?.slice(0, 8) ?? "—"}</code>
          </span>
          <span>
            <span className="tiny muted">policy</span>{" "}
            <code className="mono">{answerPolicyVersion?.slice(0, 8) ?? "—"}</code>
          </span>
        </div>
      </div>
    </section>
  );
}