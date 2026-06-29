/**
 * Softly — alternate landing experience.
 *
 * Renders inside ``App.tsx`` when the user selects the "Softly" style from
 * the top-bar style switcher. The layout is mobile-first and intended to
 * scroll as a single vertical flow: floating pill nav → hero → scenarios →
 * three-phone preview → diary entries → waitlist → FAQ → chat panel.
 *
 * Data contract:
 *   - This component is purely presentational.
 *   - The chat panel consumes the same `onSend` callback shape as the
 *     brutalist landing so we can lift message state up to App.tsx.
 *   - Citations are rendered with the same Citation[] shape used by
 *     MessageBubble.
 */

import { useEffect, useRef, useState } from "react";
import type { Citation } from "../lib/types";
import { useRevealOnScroll } from "../lib/useRevealOnScroll";

// ---------------------------------------------------------------------------
// Floating pill nav
// ---------------------------------------------------------------------------

interface SoftlyNavProps {
  onOpenChat: () => void;
}

function SoftlyNav({ onOpenChat }: SoftlyNavProps) {
  return (
    <nav className="soft-nav" aria-label="Softly primary navigation">
      <a className="soft-nav__logo" href="#top" aria-label="Softly home">
        <span className="soft-nav__logo-dot" />
      </a>
      <div className="soft-nav__links">
        <a className="soft-nav__link" href="#scenarios">Scenarios</a>
        <a className="soft-nav__link" href="#preview">App</a>
        <a className="soft-nav__link" href="#diary">Diary</a>
        <a className="soft-nav__link" href="#faq">FAQ</a>
      </div>
      <button className="soft-nav__cta" type="button" onClick={onOpenChat}>
        Try softly
      </button>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Hero
// ---------------------------------------------------------------------------

interface SoftlyHeroProps {
  onTrySample: (sample: string) => void;
}

function SoftlyHero({ onTrySample }: SoftlyHeroProps) {
  return (
    <section className="soft-hero" id="top">
      <div className="soft-hero__blob soft-hero__blob--peach" aria-hidden="true" />
      <div className="soft-hero__blob soft-hero__blob--lavender" aria-hidden="true" />

      <div className="soft-hero__inner">
        <span className="soft-hero__eyebrow soft-reveal">a calmer way to read AI answers</span>

        <h1 className="soft-hero__headline soft-reveal">
          Cited answers for AI dev tools,{" "}
          <span className="soft-hero__cursive">quietly</span>
        </h1>

        <p className="soft-hero__sub soft-reveal">
          Softly wraps CiteVyn in a low-velocity, paper-feel interface so you can scroll,
          sample, and ask without the dopamine spikes.
        </p>

        <div className="soft-hero__cta-row soft-reveal">
          <button
            type="button"
            className="soft-pill soft-pill--primary"
            onClick={() => onTrySample("How do I wire CiteVyn into Claude Code?")}
          >
            Try a sample
          </button>
          <a className="soft-pill soft-pill--secondary" href="#preview">
            See the app
          </a>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Horizontal scenario scroll
// ---------------------------------------------------------------------------

const SCENARIOS = [
  {
    time: "08:14",
    text: "Compare the warm-warmness between Readwise and CiteVyn for nightly reading.",
  },
  {
    time: "11:02",
    text: "Pull the source for the caching note in the CiteVyn backend README.",
  },
  {
    time: "13:48",
    text: "Why does the orchestrator return an empty citations array on cache hit?",
  },
  {
    time: "16:21",
    text: "Draft a release note for the new exact-search route.",
  },
  {
    time: "19:55",
    text: "Find three places where the docs mention chunk size.",
  },
];

function SoftlyScenarios() {
  return (
    <section className="soft-section" id="scenarios">
      <header className="soft-section__head soft-reveal">
        <span className="soft-section__eyebrow">scenarios</span>
        <h2 className="soft-section__title">
          Small questions, <em>softly</em> answered.
        </h2>
      </header>

      <div className="soft-scroll">
        {SCENARIOS.map((scenario, index) => (
          <article key={index} className="soft-scroll-card soft-reveal">
            <span className="soft-scroll-card__time">{scenario.time}</span>
            <p className="soft-scroll-card__text">{scenario.text}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Three-phone preview
// ---------------------------------------------------------------------------

function SoftlyPhones() {
  return (
    <section className="soft-section" id="preview">
      <header className="soft-section__head soft-reveal">
        <span className="soft-section__eyebrow">in-app</span>
        <h2 className="soft-section__title">
          Three screens. <em>One breath.</em>
        </h2>
      </header>

      <div className="soft-phones">
        {/* Left phone — sage */}
        <article className="soft-phone soft-phone--left soft-reveal">
          <div className="soft-phone__screen soft-phone__screen--sage">
            <div className="soft-phone__header">
              <span>today</span>
              <span>•</span>
            </div>
            <h3 className="soft-phone__title">Saved for tonight</h3>
            <p className="soft-phone__sub">3 cards queued for the slow hour.</p>
            <div className="soft-phone__card">
              "Why does the orchestrator fall back to cache?"
            </div>
            <div className="soft-phone__card">
              "Where is the README for the new exact-search route?"
            </div>
            <div className="soft-phone__card">
              "Compare retrieval strategies across Claude and Codex."
            </div>
          </div>
        </article>

        {/* Center phone — main breath button */}
        <article className="soft-phone soft-phone--center soft-reveal">
          <div className="soft-phone__screen soft-phone__screen--white">
            <div className="soft-phone__header">
              <span>breathe</span>
              <span>3 / 5</span>
            </div>
            <h3 className="soft-phone__title">Inhale, ask, exhale.</h3>
            <p className="soft-phone__sub">
              Press the coral disk when you're ready to send. CiteVyn answers
              in your time, not in real-time.
            </p>
            <button className="soft-phone__breath" type="button">
              <span>Breathe</span>
              <span className="soft-phone__breath-hint">tap to ask</span>
            </button>
            <div className="soft-phone__row">
              <span className="soft-phone__row-dot" />
              <span>Answer ready in ~3s</span>
            </div>
          </div>
        </article>

        {/* Right phone — lavender */}
        <article className="soft-phone soft-phone--right soft-reveal">
          <div className="soft-phone__screen soft-phone__screen--lavender">
            <div className="soft-phone__header">
              <span>last</span>
              <span>•</span>
            </div>
            <h3 className="soft-phone__title">The answer, cited.</h3>
            <p className="soft-phone__sub">3 sources · 2 chunks · 1 paraphrase.</p>
            <div className="soft-phone__row">
              <span className="soft-phone__row-dot" />
              <span>[1] backend/app/answer/orchestrator.py</span>
            </div>
            <div className="soft-phone__row">
              <span className="soft-phone__row-dot" />
              <span>[2] docs/architecture.md</span>
            </div>
            <div className="soft-phone__row">
              <span className="soft-phone__row-dot" />
              <span>[3] README.md</span>
            </div>
          </div>
        </article>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Diary testimonials
// ---------------------------------------------------------------------------

const DIARY_ENTRIES = [
  {
    body: "I used to read the citation index at 2 a.m. and feel behind. CiteVyn made the citations feel like a letter, not a log.",
    sign: "— Mira, backend engineer",
    tilt: "left" as const,
  },
  {
    body: "The wrapper UI is so quiet I actually finish reading the answer. That's the whole point, isn't it?",
    sign: "— Theo, technical writer",
    tilt: "right" as const,
  },
  {
    body: "It's the only dev tool I keep open on my phone. The breath button is a tiny joke but I use it.",
    sign: "— Ade, staff ML eng.",
    tilt: "left" as const,
  },
  {
    body: "We replaced three internal docs with one CiteVyn search. The citations are why legal signed off.",
    sign: "— Priya, platform lead",
    tilt: "right" as const,
  },
];

function SoftlyDiary() {
  return (
    <section className="soft-section" id="diary">
      <header className="soft-section__head soft-reveal">
        <span className="soft-section__eyebrow">diary</span>
        <h2 className="soft-section__title">
          Notes from people who <em>quietly</em> shipped with CiteVyn.
        </h2>
      </header>

      <div className="soft-diary">
        {DIARY_ENTRIES.map((entry, index) => (
          <article
            key={index}
            className={`soft-diary-card soft-diary-card--tilt-${entry.tilt} soft-reveal`}
          >
            <p className="soft-diary-card__body">{entry.body}</p>
            <div className="soft-diary-card__divider" />
            <span className="soft-diary-card__signature">{entry.sign}</span>
          </article>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Waitlist
// ---------------------------------------------------------------------------

function SoftlyWaitlist() {
  return (
    <section className="soft-waitlist" id="waitlist">
      <div className="soft-waitlist__blob soft-waitlist__blob--left" aria-hidden="true" />
      <div className="soft-waitlist__blob soft-waitlist__blob--right" aria-hidden="true" />

      <div className="soft-waitlist__inner soft-reveal">
        <div className="soft-waitlist__icon" aria-hidden="true">
          <span className="soft-waitlist__icon-dot" />
        </div>

        <h2 className="soft-waitlist__title">Be the first to <em>softly</em> try it.</h2>
        <p className="soft-waitlist__sub">
          Drop your email — we'll send a quiet invite when the new wrapper ships.
        </p>

        <form
          className="soft-waitlist__form"
          onSubmit={(event) => event.preventDefault()}
        >
          <label className="sr-only" htmlFor="soft-email">
            Email address
          </label>
          <input
            id="soft-email"
            type="email"
            placeholder="you@somewhere.calm"
            className="soft-waitlist__input"
            autoComplete="email"
          />
          <button type="submit" className="soft-waitlist__btn">
            Join the waitlist
          </button>
        </form>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// FAQ
// ---------------------------------------------------------------------------

const FAQ_ITEMS = [
  {
    q: "Is this a separate product?",
    a: "No. Softly is a wrapper around the same CiteVyn backend — same citations, same retrieval, just a calmer skin. Switch styles any time from the top bar.",
  },
  {
    q: "Why does the chat panel feel slower on purpose?",
    a: "Reduced motion and a longer base duration (480ms) keep the layout from snapping. Reduced-motion users get an instant reveal instead.",
  },
  {
    q: "Does Softly still expose all citations?",
    a: "Yes. Every answer carries the same Citation[] payload as the brutalist view; we just render them as small coral pills instead of stacked cards.",
  },
  {
    q: "Will the dark mode be available?",
    a: "Softly is currently a light-only experience because the grain overlay is tuned for a warm-paper base. Dark mode is on the roadmap.",
  },
  {
    q: "Can I ship a Softly-styled app from the CiteVyn API?",
    a: "Yes — SoftlyApp.tsx is fully self-contained CSS. Drop it into any React + Vite project and point it at /v1/sessions/{id}/messages.",
  },
];

function SoftlyFaq() {
  const [openIndex, setOpenIndex] = useState<number | null>(0);

  return (
    <section className="soft-section" id="faq">
      <header className="soft-section__head soft-reveal">
        <span className="soft-section__eyebrow">FAQ</span>
        <h2 className="soft-section__title">
          Questions, <em>softly</em> answered.
        </h2>
      </header>

      <div className="soft-faq">
        {FAQ_ITEMS.map((item, index) => {
          const isOpen = openIndex === index;
          return (
            <article
              key={index}
              className={`soft-faq__item soft-reveal${isOpen ? " soft-faq__item--open" : ""}`}
            >
              <button
                type="button"
                className="soft-faq__header"
                aria-expanded={isOpen}
                onClick={() => setOpenIndex(isOpen ? null : index)}
              >
                <span>{item.q}</span>
                <span className="soft-faq__icon" aria-hidden="true">
                  +
                </span>
              </button>
              <div className="soft-faq__content">
                <p className="soft-faq__answer">{item.a}</p>
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Chat panel — same API contract as ChatView
// ---------------------------------------------------------------------------

export interface SoftlyChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  inFlight?: boolean;
  error?: string;
}

interface SoftlyChatPanelProps {
  messages: SoftlyChatMessage[];
  onSend: (text: string) => void;
  onRetry?: () => void;
  isBusy: boolean;
  sessionId: string | null;
}

function SoftlyChatPanel({
  messages,
  onSend,
  onRetry,
  isBusy,
  sessionId,
}: SoftlyChatPanelProps) {
  const [draft, setDraft] = useState("");
  const composerRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const ta = composerRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
  }, [draft]);

  const submit = () => {
    const text = draft.trim();
    if (!text || isBusy) return;
    onSend(text);
    setDraft("");
  };

  return (
    <section className="soft-chat" id="chat">
      <div className="soft-chat__inner">
        <span className="soft-chat__eyebrow">live</span>
        <h2 className="soft-chat__title">
          Ask, <em>softly.</em>
        </h2>

        <div className="soft-chat__thread" aria-live="polite">
          {messages.length === 0 ? (
            <p style={{ margin: 0, fontFamily: "var(--font-sans)", fontSize: 15, color: "var(--text-muted)" }}>
              {sessionId
                ? "Ask your first question to see a cited answer."
                : "Starting a quiet session…"}
            </p>
          ) : (
            messages.map((message) => (
              <Bubble key={message.id} message={message} onRetry={onRetry} />
            ))
          )}
        </div>

        <div className="soft-composer">
          <textarea
            ref={composerRef}
            className="soft-composer__textarea"
            placeholder="Type a question, then take a breath…"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                submit();
              }
            }}
            rows={1}
          />
          <button
            type="button"
            className="soft-composer__send"
            onClick={submit}
            disabled={isBusy || draft.trim().length === 0}
            aria-label="Send message"
          >
            ↑
          </button>
        </div>
      </div>
    </section>
  );
}

function Bubble({
  message,
  onRetry,
}: {
  message: SoftlyChatMessage;
  onRetry?: () => void;
}) {
  if (message.role === "user") {
    return <div className="soft-bubble soft-bubble--user">{message.text}</div>;
  }

  if (message.inFlight) {
    return (
      <div className="soft-bubble soft-bubble--assistant" style={{ opacity: 0.7 }}>
        <span style={{ letterSpacing: "0.18em" }}>· · ·</span>
      </div>
    );
  }

  if (message.error) {
    return (
      <div className="soft-bubble soft-bubble--assistant">
        {message.error}
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            style={{
              marginLeft: "var(--space-3)",
              background: "transparent",
              border: 0,
              color: "var(--accent)",
              fontFamily: "var(--font-sans)",
              fontSize: 13,
              cursor: "pointer",
            }}
          >
            retry
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="soft-bubble soft-bubble--assistant">
      <div>{message.text}</div>
      {message.citations && message.citations.length > 0 && (
        <div className="soft-bubble__citations">
          {message.citations.map((citation, index) => (
            <a
              key={citation.chunk_id ?? `${citation.url}-${index}`}
              className="soft-bubble__cite"
              href={citation.url ?? "#"}
              target="_blank"
              rel="noreferrer"
              title={citation.title ?? citation.source_name ?? ""}
            >
              {index + 1}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Root Softly shell
// ---------------------------------------------------------------------------

export interface SoftlyAppProps {
  messages: SoftlyChatMessage[];
  onSend: (text: string) => void;
  onRetry?: () => void;
  isBusy: boolean;
  sessionId: string | null;
}

export function SoftlyApp({
  messages,
  onSend,
  onRetry,
  isBusy,
  sessionId,
}: SoftlyAppProps) {
  const shellRef = useRevealOnScroll();

  const scrollToChat = () => {
    document.getElementById("chat")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const handleSample = (sample: string) => {
    onSend(sample);
    scrollToChat();
  };

  return (
    <div className="soft-shell" ref={shellRef as React.RefObject<HTMLDivElement>}>
      <SoftlyNav onOpenChat={scrollToChat} />

      <main className="soft-shell__main">
        <SoftlyHero onTrySample={handleSample} />
        <SoftlyScenarios />
        <SoftlyPhones />
        <SoftlyDiary />
        <SoftlyWaitlist />
        <SoftlyFaq />
        <SoftlyChatPanel
          messages={messages}
          onSend={onSend}
          onRetry={onRetry}
          isBusy={isBusy}
          sessionId={sessionId}
        />
      </main>

      <div className="soft-grain" aria-hidden="true" />
    </div>
  );
}