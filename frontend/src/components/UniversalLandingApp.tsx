/**
 * UniversalLandingApp — Professional landing page with 2 UI alternatives.
 *
 * @see ../styles/universal-landing.css
 *
 * UI Option 1: Browser-Core Modernism
 * - DevTools aesthetic with browser chrome frame
 * - Pattern grid background, monospace labels
 * - Technical, high-density layout
 *
 * UI Option 2: Bold Editorial Studio
 * - Typography-first with black/white palette
 * - Custom cursor, smooth animations
 * - Creative, editorial layout
 *
 * Both options include:
 * - Light/dark theme toggle
 * - How it Works section (3 steps)
 * - Interactive demo
 * - FAQ accordion
 */

import "../styles/universal-landing.css";

import { useCallback, useEffect, useRef, useState } from "react";

import type { AskResponse, SessionId } from "../lib/types";
import type { ApiClientError } from "../lib/types";

import { ChatView } from "./ChatView";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type UIVariant = "browser-core" | "editorial-studio";

interface UniversalLandingAppProps {
  sessionId: SessionId | null;
  sessionStartedAt: string | null;
  messageCount: number;
  indexVersion: string | null;
  answerPolicyVersion: string | null;
  onSessionCreated: (id: SessionId) => void;
  onError: (err: ApiClientError) => void;
  onResponseMetadata: (response: AskResponse) => void;
  onNewSession: () => void;
  onSwitchView: (view: "chat" | "exact" | "about") => void;
}

// Sample chunks for demo
const DEMO_CHUNKS = [
  {
    id: "chunk-01",
    source: "docs/rag/architecture.md §2.4",
    span: "§2.4:14–§2.4:38",
    score: 0.94,
    text: "Chunking splits the corpus into 200–400 token windows with a 20-token overlap. Each window keeps its source path and byte span so the model can cite it back.",
  },
  {
    id: "chunk-02",
    source: "docs/rag/scoring.md §1.1",
    span: "§1.1:03–§1.1:22",
    score: 0.87,
    text: "Cosine similarity is computed against the query embedding. Scores above 0.6 require the model to attach a citation; below 0.6 the answer is marked unverified.",
  },
  {
    id: "chunk-03",
    source: "docs/verification/policy.md §3.2",
    span: "§3.2:01–§3.2:15",
    score: 0.71,
    text: "When no source chunk exceeds the confidence threshold, CiteVyn returns a refusal rather than generating an unverified answer.",
  },
];

// Demo questions
const DEMO_QUESTIONS = [
  "How does chunking work in CiteVyn?",
  "What is the rate limit policy?",
  "How are citations scored?",
  "What happens when confidence is low?",
];

// FAQ data
const FAQ_DATA = [
  {
    question: "How does CiteVyn ensure answers are accurate?",
    answer: "CiteVyn retrieves relevant passages from indexed documentation using semantic search. The model must cite the exact passage it used for each claim. If no passage meets the confidence threshold, CiteVyn explicitly says it doesn't know rather than guessing.",
  },
  {
    question: "What AI providers are supported?",
    answer: "CiteVyn supports Anthropic's Claude, Claude Code, OpenAI's Codex, and Google's Gemini. The citation verification layer works identically across all providers.",
  },
  {
    question: "How fast are the responses?",
    answer: "Typical response times are 2-5 seconds for the retrieval step, plus the LLM generation time (usually 1-3 seconds). The demo allows 30 requests per hour to ensure fair access.",
  },
  {
    question: "Is my data being logged?",
    answer: "No. CiteVyn only indexes public documentation. No user queries or conversation data are stored. The rate limiter uses anonymous session tokens.",
  },
  {
    question: "Can I use my own documentation?",
    answer: "Currently CiteVyn indexes official documentation for supported AI tools. Enterprise self-hosted deployments can index custom documentation.",
  },
];

// ---------------------------------------------------------------------------
// Custom Hooks
// ---------------------------------------------------------------------------

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    setReduced(mq.matches);
    const handler = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return reduced;
}

function useInView(ref: React.RefObject<Element | null>, threshold = 0.1): boolean {
  const [inView, setInView] = useState(false);
  useEffect(() => {
    if (!ref.current) return;
    const observer = new IntersectionObserver(
      ([entry]) => setInView(entry.isIntersecting),
      { threshold }
    );
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, [ref, threshold]);
  return inView;
}

// ---------------------------------------------------------------------------
// Theme & UI Variant Context
// ---------------------------------------------------------------------------

const STORAGE_KEY_UI = "citevyn:landing-ui";
const STORAGE_KEY_THEME = "citevyn:landing-theme";

export function UniversalLandingApp(props: UniversalLandingAppProps) {
  const [uiVariant, setUiVariant] = useState<UIVariant>(() => {
    const stored = localStorage.getItem(STORAGE_KEY_UI);
    return stored === "browser-core" || stored === "editorial-studio"
      ? stored
      : "browser-core";
  });

  const [theme, setTheme] = useState<"light" | "dark">(() => {
    const stored = localStorage.getItem(STORAGE_KEY_THEME);
    if (stored === "light" || stored === "dark") return stored;
    return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  });

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY_UI, uiVariant);
    document.documentElement.setAttribute("data-landing-ui", uiVariant);
  }, [uiVariant]);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY_THEME, theme);
    document.documentElement.setAttribute("data-landing-theme", theme);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((t) => (t === "dark" ? "light" : "dark"));
  }, []);

  const toggleUI = useCallback(() => {
    setUiVariant((v) => (v === "browser-core" ? "editorial-studio" : "browser-core"));
  }, []);

  return (
    <div className={`landing landing--${uiVariant} landing--${theme}`} data-testid="landing-container">
      {/* Floating Controls */}
      <div className="landing__controls">
        <button
          type="button"
          className="landing__control-btn"
          onClick={toggleUI}
          aria-label={`Switch to ${uiVariant === "browser-core" ? "Editorial" : "Browser"} UI`}
          data-testid="ui-toggle-button"
        >
          {uiVariant === "browser-core" ? "Editorial" : "Browser"} UI
        </button>
        <button
          type="button"
          className="landing__control-btn landing__control-btn--theme"
          onClick={toggleTheme}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          data-testid="theme-toggle-button"
        >
          {theme === "dark" ? "☀" : "☾"}
        </button>
      </div>

      {uiVariant === "browser-core" ? (
        <BrowserCoreLanding {...props} theme={theme} />
      ) : (
        <EditorialStudioLanding {...props} theme={theme} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Browser-Core Modernism Landing
// ---------------------------------------------------------------------------

function BrowserCoreLanding(
  props: UniversalLandingAppProps & { theme: "light" | "dark" }
) {
  const { theme } = props;
  const [activeSection, setActiveSection] = useState<"how" | "demo" | "faq">("how");
  const [selectedQuestion, setSelectedQuestion] = useState<string | null>(null);
  const [activeChunk, setActiveChunk] = useState(DEMO_CHUNKS[0].id);
  const [chatMode, setChatMode] = useState(false);

  return (
    <div className="bc-landing">
      {/* Browser Chrome Header */}
      <header className="bc-chrome">
        <div className="bc-chrome__traffic-lights">
          <span className="bc-chrome__dot bc-chrome__dot--red" />
          <span className="bc-chrome__dot bc-chrome__dot--yellow" />
          <span className="bc-chrome__dot bc-chrome__dot--green" />
        </div>
        <div className="bc-chrome__tabs">
          <button
            type="button"
            className={`bc-chrome__tab ${!chatMode ? "bc-chrome__tab--active" : ""}`}
            onClick={() => setChatMode(false)}
          >
            citevyn.ai
          </button>
          {chatMode && (
            <button
              type="button"
              className="bc-chrome__tab bc-chrome__tab--active"
            >
              /api/v1/ask
            </button>
          )}
        </div>
        <div className="bc-chrome__address">
          <span className="bc-chrome__url">https://citevyn.ai</span>
          <span className="bc-chrome__status">● live</span>
        </div>
        <div className="bc-chrome__extensions">
          <span className="bc-chrome__ext" title="CiteVyn">CV</span>
        </div>
      </header>

      {!chatMode ? (
        <>
          {/* Hero Section */}
          <section className="bc-hero">
            <div className="bc-hero__pattern" />
            <div className="bc-hero__content">
              <div className="bc-hero__badge">
                <span className="bc-hero__badge-dot" />
                <span className="bc-hero__badge-text">v2.0 RELEASED</span>
              </div>
              <h1 className="bc-hero__headline">
                Cited answers for{" "}
                <span className="bc-hero__accent">AI dev tools.</span>
              </h1>
              <p className="bc-hero__sub">
                CiteVyn retrieves official documentation and shows you the exact
                chunks it cited — score, source, and span — every time.
              </p>
              <div className="bc-hero__ctas">
                <button
                  type="button"
                  className="bc-btn bc-btn--primary"
                  onClick={() => setChatMode(true)}
                >
                  Try the demo →
                </button>
                <button
                  type="button"
                  className="bc-btn bc-btn--secondary"
                  onClick={() => setActiveSection("how")}
                >
                  See how it works
                </button>
              </div>
              <div className="bc-hero__stats">
                <div className="bc-hero__stat">
                  <span className="bc-hero__stat-value">99.2%</span>
                  <span className="bc-hero__stat-label">citation rate</span>
                </div>
                <div className="bc-hero__stat">
                  <span className="bc-hero__stat-value">3</span>
                  <span className="bc-hero__stat-label">AI providers</span>
                </div>
                <div className="bc-hero__stat">
                  <span className="bc-hero__stat-value">142ms</span>
                  <span className="bc-hero__stat-label">avg retrieval</span>
                </div>
              </div>
            </div>

            {/* Interactive Demo Preview */}
            <div className="bc-hero__demo">
              <div className="bc-demo-window">
                <div className="bc-demo-window__header">
                  <span className="bc-demo-window__title">citation trace</span>
                  <span className="bc-demo-window__meta">3 chunks · 142ms</span>
                </div>
                <div className="bc-demo-window__chunks">
                  {DEMO_CHUNKS.map((chunk, i) => (
                    <div
                      key={chunk.id}
                      className={`bc-chunk-card ${activeChunk === chunk.id ? "bc-chunk-card--active" : ""}`}
                      onClick={() => setActiveChunk(chunk.id)}
                    >
                      <div className="bc-chunk-card__header">
                        <span className="bc-chunk-card__tag">[{i + 1}]</span>
                        <span className="bc-chunk-card__source">{chunk.source}</span>
                        <span className="bc-chunk-card__score">
                          score {chunk.score.toFixed(2)}
                        </span>
                      </div>
                      {activeChunk === chunk.id && (
                        <p className="bc-chunk-card__text">{chunk.text}</p>
                      )}
                    </div>
                  ))}
                </div>
                <div className="bc-demo-window__answer">
                  <span className="bc-demo-window__label">assistant</span>
                  <p>
                    CiteVyn uses semantic chunking with{" "}
                    <span
                      className={`bc-cite ${activeChunk === "chunk-01" ? "bc-cite--active" : ""}`}
                      onClick={() => setActiveChunk("chunk-01")}
                    >
                      [1]
                    </span>{" "}
                    token overlap and computes cosine similarity{" "}
                    <span
                      className={`bc-cite ${activeChunk === "chunk-02" ? "bc-cite--active" : ""}`}
                      onClick={() => setActiveChunk("chunk-02")}
                    >
                      [2]
                    </span>
                    .
                  </p>
                </div>
              </div>
            </div>
          </section>

          {/* Navigation Tabs */}
          <nav className="bc-nav">
            <button
              type="button"
              className={`bc-nav__tab ${activeSection === "how" ? "bc-nav__tab--active" : ""}`}
              onClick={() => setActiveSection("how")}
            >
              How it works
            </button>
            <button
              type="button"
              className={`bc-nav__tab ${activeSection === "demo" ? "bc-nav__tab--active" : ""}`}
              onClick={() => {
                setActiveSection("demo");
                setChatMode(true);
              }}
            >
              Try the demo
            </button>
            <button
              type="button"
              className={`bc-nav__tab ${activeSection === "faq" ? "bc-nav__tab--active" : ""}`}
              onClick={() => setActiveSection("faq")}
            >
              FAQ
            </button>
          </nav>

          {/* How It Works Section */}
          <section className={`bc-section ${activeSection === "how" ? "bc-section--active" : ""}`}>
            <div className="bc-section__header">
              <h2 className="bc-section__title">How it works</h2>
              <p className="bc-section__sub">Three steps from question to cited answer</p>
            </div>

            <div className="bc-steps">
              <div className="bc-step">
                <div className="bc-step__number">01</div>
                <div className="bc-step__content">
                  <h3 className="bc-step__title">Query Embedding</h3>
                  <p className="bc-step__desc">
                    Your question is converted to a vector embedding using
                    state-of-the-art models. This captures the semantic meaning,
                    not just keywords.
                  </p>
                  <div className="bc-step__example">
                    <span className="bc-step__example-label">Example:</span>
                    <code className="bc-step__code">
                      "How do I install Claude Code?" → [0.23, -0.41, ...]
                    </code>
                  </div>
                </div>
              </div>

              <div className="bc-step">
                <div className="bc-step__number">02</div>
                <div className="bc-step__content">
                  <h3 className="bc-step__title">Semantic Retrieval</h3>
                  <p className="bc-step__desc">
                    The embedding is compared against indexed documentation chunks
                    using cosine similarity. Top matches are retrieved with scores.
                  </p>
                  <div className="bc-step__example">
                    <span className="bc-step__example-label">Example:</span>
                    <code className="bc-step__code">
                      Retrieved 3 chunks (scores: 0.94, 0.87, 0.71)
                    </code>
                  </div>
                </div>
              </div>

              <div className="bc-step">
                <div className="bc-step__number">03</div>
                <div className="bc-step__content">
                  <h3 className="bc-step__title">Citation Generation</h3>
                  <p className="bc-step__desc">
                    The LLM generates an answer using only the retrieved chunks.
                    Every claim is linked to its source with a confidence score.
                  </p>
                  <div className="bc-step__example">
                    <span className="bc-step__example-label">Example:</span>
                    <code className="bc-step__code">
                      "Run `npm install -g @anthropic-ai/claude-code`" [1]
                    </code>
                  </div>
                </div>
              </div>
            </div>

            {/* Interactive Process Demo */}
            <div className="bc-process-demo">
              <h3 className="bc-process-demo__title">Try it yourself</h3>
              <div className="bc-process-demo__query">
                <span className="bc-process-demo__label">Q:</span>
                <select
                  className="bc-process-demo__select"
                  value={selectedQuestion || ""}
                  onChange={(e) => setSelectedQuestion(e.target.value)}
                >
                  <option value="">Select a question...</option>
                  {DEMO_QUESTIONS.map((q) => (
                    <option key={q} value={q}>{q}</option>
                  ))}
                </select>
              </div>
              {selectedQuestion && (
                <div className="bc-process-demo__result">
                  <div className="bc-process-demo__chunks">
                    {DEMO_CHUNKS.map((chunk, i) => (
                      <div key={chunk.id} className="bc-process-demo__chunk">
                        <span className="bc-process-demo__chunk-tag">[{i + 1}]</span>
                        <span className="bc-process-demo__chunk-source">{chunk.source}</span>
                        <span className="bc-process-demo__chunk-score">
                          {chunk.score.toFixed(2)}
                        </span>
                      </div>
                    ))}
                  </div>
                  <div className="bc-process-demo__answer">
                    <span className="bc-process-demo__answer-label">A:</span>
                    <p>
                      {selectedQuestion.includes("chunking")
                        ? "CiteVyn splits documentation into 200-400 token chunks with 20-token overlap [1]. Each chunk is scored for relevance [2]. Low-confidence queries trigger a refusal rather than a guess [3]."
                        : selectedQuestion.includes("rate")
                        ? "The rate limiter uses Redis with a sliding window of 30 requests per hour per session."
                        : "Citations are required when the relevance score exceeds 0.6 [2]. Below this threshold, answers are marked as unverified."}
                    </p>
                  </div>
                </div>
              )}
            </div>
          </section>

          {/* Demo Section */}
          <section className={`bc-section ${activeSection === "demo" ? "bc-section--active" : ""}`}>
            <div className="bc-section__header">
              <h2 className="bc-section__title">Try the demo</h2>
              <p className="bc-section__sub">Ask anything about Claude, Claude Code, Codex, or Gemini</p>
            </div>

            <div className="bc-demo-questions">
              {DEMO_QUESTIONS.map((q) => (
                <button
                  key={q}
                  type="button"
                  className="bc-demo-q"
                  onClick={() => {
                    setSelectedQuestion(q);
                    setChatMode(true);
                  }}
                >
                  {q}
                </button>
              ))}
            </div>

            <button
              type="button"
              className="bc-btn bc-btn--primary bc-demo-cta"
              onClick={() => setChatMode(true)}
            >
              Open full demo →
            </button>
          </section>

          {/* FAQ Section */}
          <section className={`bc-section ${activeSection === "faq" ? "bc-section--active" : ""}`}>
            <div className="bc-section__header">
              <h2 className="bc-section__title">FAQ</h2>
              <p className="bc-section__sub">Common questions about CiteVyn</p>
            </div>

            <div className="bc-faq">
              {FAQ_DATA.map((item, i) => (
                <details key={i} className="bc-faq__item">
                  <summary className="bc-faq__question">{item.question}</summary>
                  <p className="bc-faq__answer">{item.answer}</p>
                </details>
              ))}
            </div>
          </section>

          {/* Footer */}
          <footer className="bc-footer">
            <div className="bc-footer__content">
              <span className="bc-footer__brand">citevyn</span>
              <span className="bc-footer__copy">Citation-grounded Q&A for AI tools</span>
            </div>
          </footer>
        </>
      ) : (
        /* Chat Mode */
        <div className="bc-chat-wrapper">
          <ChatView
            sessionId={props.sessionId}
            sessionStartedAt={props.sessionStartedAt}
            messageCount={props.messageCount}
            indexVersion={props.indexVersion}
            answerPolicyVersion={props.answerPolicyVersion}
            onSessionCreated={props.onSessionCreated}
            onError={props.onError}
            onResponseMetadata={props.onResponseMetadata}
            onNewSession={props.onNewSession}
            onSwitchView={props.onSwitchView}
          />
          <button
            type="button"
            className="bc-back-btn"
            onClick={() => setChatMode(false)}
          >
            ← Back to landing
          </button>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Bold Editorial Studio Landing
// ---------------------------------------------------------------------------

function EditorialStudioLanding(
  props: UniversalLandingAppProps & { theme: "light" | "dark" }
) {
  const { theme } = props;
  const reducedMotion = useReducedMotion();
  const heroRef = useRef<HTMLDivElement>(null);
  const heroInView = useInView(heroRef as React.RefObject<Element | null>);
  const [activeSection, setActiveSection] = useState<"how" | "demo" | "faq">("how");
  const [selectedQuestion, setSelectedQuestion] = useState<string | null>(null);
  const [chatMode, setChatMode] = useState(false);
  const [showCursor, setShowCursor] = useState(false);
  const cursorRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 });
  const cursorStyleRef = useRef<{ x: number; y: number }>({ x: 0, y: 0 });
  const rafRef = useRef<number>(0);

  // Custom cursor interpolation
  useEffect(() => {
    if (reducedMotion) return;

    const lerp = (a: number, b: number, t: number) => a + (b - a) * t;

    const animate = () => {
      cursorStyleRef.current.x = lerp(cursorStyleRef.current.x, cursorRef.current.x, 0.15);
      cursorStyleRef.current.y = lerp(cursorStyleRef.current.y, cursorRef.current.y, 0.15);

      const cursor = document.querySelector(".es-cursor") as HTMLElement;
      if (cursor) {
        cursor.style.transform = `translate(${cursorStyleRef.current.x}px, ${cursorStyleRef.current.y}px)`;
      }

      rafRef.current = requestAnimationFrame(animate);
    };

    rafRef.current = requestAnimationFrame(animate);

    return () => cancelAnimationFrame(rafRef.current);
  }, [reducedMotion]);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    cursorRef.current = { x: e.clientX - 16, y: e.clientY - 16 };
    setShowCursor(true);
  }, []);

  const handleMouseLeave = useCallback(() => setShowCursor(false), []);

  return (
    <div
      className="es-landing"
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
      style={{ cursor: "none" }}
    >
      {/* Custom Cursor */}
      {!reducedMotion && (
        <div
          className={`es-cursor ${showCursor ? "es-cursor--visible" : ""}`}
          aria-hidden="true"
        />
      )}

      {/* Navigation */}
      <header className="es-header">
        <a href="#" className="es-logo">citevyn</a>
        <nav className="es-nav">
          <button
            type="button"
            className="es-nav__link"
            onClick={() => setActiveSection("how")}
          >
            How it works
          </button>
          <button
            type="button"
            className="es-nav__link"
            onClick={() => {
              setActiveSection("demo");
              setChatMode(true);
            }}
          >
            Demo
          </button>
          <button
            type="button"
            className="es-nav__link"
            onClick={() => setActiveSection("faq")}
          >
            FAQ
          </button>
        </nav>
      </header>

      {!chatMode ? (
        <>
          {/* Hero */}
          <section className="es-hero" ref={heroRef}>
            <h1 className="es-hero__headline">
              {"Citation-grounded".split("").map((char, i) => (
                <span
                  key={i}
                  className={`es-hero__char ${!reducedMotion && heroInView ? "es-hero__char--visible" : ""}`}
                  style={{ transitionDelay: `${i * 30}ms` }}
                >
                  {char === " " ? " " : char}
                </span>
              ))}
              <br />
              {"Q&A for AI".split("").map((char, i) => (
                <span
                  key={i}
                  className={`es-hero__char ${!reducedMotion && heroInView ? "es-hero__char--visible" : ""}`}
                  style={{ transitionDelay: `${(i + 20) * 30}ms` }}
                >
                  {char === " " ? " " : char}
                </span>
              ))}
              <br />
              {"tools.".split("").map((char, i) => (
                <span
                  key={i}
                  className={`es-hero__char es-hero__char--accent ${!reducedMotion && heroInView ? "es-hero__char--visible" : ""}`}
                  style={{ transitionDelay: `${(i + 32) * 30}ms` }}
                >
                  {char}
                </span>
              ))}
            </h1>
            <p className="es-hero__sub">
              Ask questions about Claude, Claude Code, Codex, and Gemini.
              Every answer cites the exact documentation it came from.
            </p>
            <div className="es-hero__ctas">
              <button
                type="button"
                className="es-btn es-btn--primary"
                onClick={() => setChatMode(true)}
              >
                Try the demo
              </button>
              <button
                type="button"
                className="es-btn es-btn--secondary"
                onClick={() => setActiveSection("how")}
              >
                See how it works
              </button>
            </div>
          </section>

          {/* Marquee */}
          <div className="es-marquee">
            <div className="es-marquee__track">
              {[...Array(3)].map((_, i) => (
                <div key={i} className="es-marquee__group">
                  <span className="es-marquee__item">Semantic search</span>
                  <span className="es-marquee__sep">×</span>
                  <span className="es-marquee__item">Verified citations</span>
                  <span className="es-marquee__sep">×</span>
                  <span className="es-marquee__item">No hallucinations</span>
                  <span className="es-marquee__sep">×</span>
                  <span className="es-marquee__item">Instant answers</span>
                  <span className="es-marquee__sep">×</span>
                </div>
              ))}
            </div>
          </div>

          {/* Stats */}
          <section className="es-stats">
            <div className="es-stat">
              <span className="es-stat__value">99.2%</span>
              <span className="es-stat__label">Citation Rate</span>
            </div>
            <div className="es-stat">
              <span className="es-stat__value">3</span>
              <span className="es-stat__label">AI Providers</span>
            </div>
            <div className="es-stat">
              <span className="es-stat__value">142ms</span>
              <span className="es-stat__label">Avg Retrieval</span>
            </div>
            <div className="es-stat">
              <span className="es-stat__value">0</span>
              <span className="es-stat__label">Data Logged</span>
            </div>
          </section>

          {/* How It Works */}
          <section className={`es-section ${activeSection === "how" ? "es-section--active" : ""}`}>
            <h2 className="es-section__title">How it answers your question</h2>

            <div className="es-steps">
              <div className="es-step">
                <span className="es-step__num">01</span>
                <div className="es-step__content">
                  <h3 className="es-step__title">Embed Your Query</h3>
                  <p className="es-step__desc">
                    Your question is converted to a semantic vector that captures
                    meaning, not just keywords.
                  </p>
                </div>
              </div>

              <div className="es-step">
                <span className="es-step__num">02</span>
                <div className="es-step__content">
                  <h3 className="es-step__title">Find Relevant Chunks</h3>
                  <p className="es-step__desc">
                    The vector searches through indexed documentation to find
                    the most relevant passages with confidence scores.
                  </p>
                </div>
              </div>

              <div className="es-step">
                <span className="es-step__num">03</span>
                <div className="es-step__content">
                  <h3 className="es-step__title">Generate Cited Answer</h3>
                  <p className="es-step__desc">
                    The answer is generated using only verified sources.
                    Every claim links back to its documentation.
                  </p>
                </div>
              </div>
            </div>

            {/* Interactive Example */}
            <div className="es-interactive">
              <h3 className="es-interactive__title">See it in action</h3>
              <select
                className="es-select"
                value={selectedQuestion || ""}
                onChange={(e) => setSelectedQuestion(e.target.value)}
              >
                <option value="">Choose a question...</option>
                {DEMO_QUESTIONS.map((q) => (
                  <option key={q} value={q}>{q}</option>
                ))}
              </select>

              {selectedQuestion && (
                <div className="es-interactive__result">
                  <div className="es-interactive__sources">
                    <span className="es-interactive__sources-label">Sources used:</span>
                    {DEMO_CHUNKS.map((chunk, i) => (
                      <span key={chunk.id} className="es-source">
                        [{i + 1}] {chunk.source}
                      </span>
                    ))}
                  </div>
                  <p className="es-interactive__answer">
                    {selectedQuestion.includes("chunking")
                      ? "CiteVyn splits documentation into overlapping token windows (200-400 tokens each). Each window is scored for semantic relevance. Below threshold? The answer is marked unverified."
                      : selectedQuestion.includes("rate")
                      ? "CiteVyn enforces 30 requests per hour via Redis sliding window. Anonymous sessions get fair access without login."
                      : "Each passage gets a cosine similarity score. Above 0.6? Citation required. Below? Answer marked unverified — no guessing."}
                  </p>
                </div>
              )}
            </div>
          </section>

          {/* Demo CTA */}
          <section className={`es-section ${activeSection === "demo" ? "es-section--active" : ""}`}>
            <h2 className="es-section__title">Try the demo</h2>
            <p className="es-section__desc">
              No signup required. Ask anything about Claude, Claude Code, Codex, or Gemini.
            </p>

            <div className="es-demo-questions">
              {DEMO_QUESTIONS.map((q) => (
                <button
                  key={q}
                  type="button"
                  className="es-demo-q"
                  onClick={() => {
                    setSelectedQuestion(q);
                    setChatMode(true);
                  }}
                >
                  {q}
                </button>
              ))}
            </div>

            <button
              type="button"
              className="es-btn es-btn--primary"
              onClick={() => setChatMode(true)}
            >
              Open full demo →
            </button>
          </section>

          {/* FAQ */}
          <section className={`es-section ${activeSection === "faq" ? "es-section--active" : ""}`}>
            <h2 className="es-section__title">FAQ</h2>

            <div className="es-faq">
              {FAQ_DATA.map((item, i) => (
                <details key={i} className="es-faq__item">
                  <summary className="es-faq__question">{item.question}</summary>
                  <p className="es-faq__answer">{item.answer}</p>
                </details>
              ))}
            </div>
          </section>

          {/* Footer */}
          <footer className="es-footer">
            <div className="es-footer__brand">CiteVyn</div>
            <p className="es-footer__tagline">
              Citation-grounded Q&A for AI developers
            </p>
            <div className="es-footer__links">
              <span>GitHub</span>
              <span>Documentation</span>
              <span>Contact</span>
            </div>
          </footer>
        </>
      ) : (
        /* Chat Mode */
        <div className="es-chat-wrapper">
          <button
            type="button"
            className="es-back-btn"
            onClick={() => setChatMode(false)}
          >
            ← Back to landing
          </button>
          <ChatView
            sessionId={props.sessionId}
            sessionStartedAt={props.sessionStartedAt}
            messageCount={props.messageCount}
            indexVersion={props.indexVersion}
            answerPolicyVersion={props.answerPolicyVersion}
            onSessionCreated={props.onSessionCreated}
            onError={props.onError}
            onResponseMetadata={props.onResponseMetadata}
            onNewSession={props.onNewSession}
            onSwitchView={props.onSwitchView}
          />
        </div>
      )}
    </div>
  );
}
