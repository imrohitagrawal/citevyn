/**
 * Landing sections — Hero, Ticker, SourcesStrip, Personas, HowItWorks, etc.
 *
 * Each is a small, focused component. The full page template is in
 * LandingPage.tsx which imports these sub-components.
 */

import { type Source } from "../data/knowledgeBase";

// ---------------------------------------------------------------------------
// QuestionTicker
// ---------------------------------------------------------------------------

export function QuestionTicker({
  marquee,
}: {
  marquee: Array<{ q: string; tag: string; select: () => void }>;
}) {
  return (
    <section>
      <div className="ticker-strip">
        <div className="ticker-track">
          {marquee.map((mq, i) => (
            <button
              key={i}
              onClick={mq.select}
              className="ticker-chip"
            >
              <span className="ticker-tag">{mq.tag}</span>
              {mq.q}
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// SourcesStrip
// ---------------------------------------------------------------------------

export function SourcesStrip() {
  return (
    <section className="sources-strip">
      <span className="mono-label">Grounded in official documentation from</span>
      <div className="tools-row">
        {["CL", "CC", "CX", "GM"].map((g, i) => (
          <div key={g} className="tool-item">
            <span className="tool-badge">{g}</span>
            <span className="tool-name">
              {["Claude", "Claude Code", "Codex", "Gemini"][i]}
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Personas
// ---------------------------------------------------------------------------

const personas = [
  {
    tag: "01 · JUST CURIOUS",
    title: "Exploring AI tools",
    body: "Never touched a terminal? Ask what things are, what they cost, and where to start — in plain English, answered from the makers' own guides.",
    qs: ["What is Claude Code?", "Does Claude Code cost money?"],
  },
  {
    tag: "02 · SHIPPING DAILY",
    title: "Building with these tools",
    body: "Exact lookups for flags, config keys, SDK methods, and errors — a precise answer with the doc page attached, faster than digging through tabs.",
    qs: ["What does the --model flag do in Codex?", "How do I stream responses from the Gemini API?"],
  },
  {
    tag: "03 · CHOOSING FOR A TEAM",
    title: "Evaluating & deciding",
    body: "Compare capabilities across tools with answers you can forward to anyone — every claim carries a source your team can verify.",
    qs: ["Which Claude models are available in the API?", "How do I get a Gemini API key?"],
  },
];

export function Personas({
  onAsk,
}: {
  onAsk: (q: string) => void;
}) {
  return (
    <section id="who" className="section">
      <div className="section-header">
        <span className="mono-label">Who it's for</span>
        <h2>
          Whoever you are, <em>just ask.</em>
        </h2>
        <p>
          Same engine, same honesty — whether you've never opened a terminal or
          you live in one.
        </p>
      </div>
      <div className="personas-grid">
        {personas.map((p, i) => (
          <div key={i} className="persona-card">
            <span className="persona-tag">{p.tag}</span>
            <h3>{p.title}</h3>
            <p>{p.body}</p>
            <div className="persona-questions">
              <span className="mono-label">ASK THIS ↓</span>
              {p.qs.map((q, j) => (
                <button
                  key={j}
                  onClick={() => onAsk(q)}
                  className="persona-q-btn"
                >
                  <span>{q}</span>
                  <span>→</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// HowItWorks
// ---------------------------------------------------------------------------

export function HowItWorks() {
  return (
    <section id="how" className="section">
      <div className="section-header">
        <span className="mono-label">How it works</span>
        <h2>
          Three simple steps to an answer you can <em>trust</em>.
        </h2>
        <p>
          No commands, no jargon. Ask the way you'd ask a knowledgeable
          colleague — CiteVyn does the looking-up for you.
        </p>
      </div>
      <div className="steps-grid">
        {/* Step 1 */}
        <div className="step-card">
          <div className="step-preview">
            <div className="typing-preview">
              <span className="typing-prompt">›</span>
              <span className="typing-text">How do I get better answers from an AI?</span>
              <span className="typing-caret" />
            </div>
            <div className="tool-chips">
              {["Claude", "Claude Code", "Codex", "Gemini"].map((t) => (
                <span key={t} className="tool-chip">{t}</span>
              ))}
            </div>
          </div>
          <div className="step-meta">
            <span className="step-number">01</span>
            <h3>Ask in plain words</h3>
          </div>
          <p>
            Type your question the way you'd say it out loud — no special
            commands or technical terms. CiteVyn works out which tool you're
            asking about.
          </p>
        </div>

        {/* Step 2 */}
        <div className="step-card">
          <div className="step-preview">
            <div className="doc-preview">
              <div className="doc-header">
                <span className="doc-icon" />
                <span className="doc-title">The official guide</span>
                <span className="doc-page">page 3</span>
              </div>
              <div className="doc-skeleton">
                <div className="doc-line" style={{ width: "100%" }} />
                <div className="doc-line" style={{ width: "84%" }} />
                <div className="doc-line highlight-line">
                  Use --model to pick a model per run.
                </div>
                <div className="doc-line" style={{ width: "66%" }} />
              </div>
            </div>
            <div className="found-check">
              <span className="check-icon">✓</span>
              Found the exact part
            </div>
          </div>
          <div className="step-meta">
            <span className="step-number">02</span>
            <h3>We check the official guides</h3>
          </div>
          <p>
            CiteVyn reads the real manuals written by the makers of each tool
            and finds the exact part that answers you — not random pages from
            around the web.
          </p>
        </div>

        {/* Step 3 */}
        <div className="step-card">
          <div className="step-preview">
            <div className="quote-preview">
              <p>“Ask short, clear questions and give an example of what you want.”</p>
              <div className="from-guide">
                <span className="check-icon">✓</span>
                From the official guide
              </div>
            </div>
            <div className="not-covered">
              <span>⦸</span>
              Not covered? It says so — no guessing.
            </div>
          </div>
          <div className="step-meta">
            <span className="step-number">03</span>
            <h3>Get an answer you can trust</h3>
          </div>
          <p>
            You get a short, clear answer with a link to where it came from. If
            the guides don't cover your question, CiteVyn simply tells you —
            instead of making something up.
          </p>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// WhyDifferent
// ---------------------------------------------------------------------------

export function WhyDifferent() {
  return (
    <section id="why" className="section">
      <div className="section-header centered">
        <span className="mono-label">Why it's different</span>
        <h2>
          Built to say <span className="highlight">"I don't know."</span>
        </h2>
        <p>
          Same question, two very different answers. One of them you can actually
          check.
        </p>
      </div>

      <div className="compare-grid">
        {/* Generic bot */}
        <div className="compare-card generic">
          <div className="compare-header">
            <span className="compare-avatar generic-avatar">?</span>
            <span className="compare-name">A generic chatbot</span>
            <span className="source-badge zero">0 SOURCES</span>
          </div>
          <div className="compare-body">
            <p className="compare-q">"What does the --model flag do in Codex?"</p>
            <p className="compare-a">
              The --model flag lets you switch between{" "}
              <span className="invention">codex-fast and codex-max modes</span>. It
              also <span className="invention">controls the creativity of
              responses</span>, and most people should just set it to{" "}
              <span className="invention">auto</span>.
            </p>
            <div className="compare-footer bad">
              <span>✗</span>
              Sounds right. Isn't. And there's nothing to check.
            </div>
          </div>
        </div>

        {/* CiteVyn */}
        <div className="compare-card citevyn">
          <div className="compare-header">
            <span className="compare-avatar cv-avatar">CV</span>
            <span className="compare-name">CiteVyn</span>
            <span className="source-badge one">1 SOURCE</span>
          </div>
          <div className="compare-body">
            <p className="compare-q">"What does the --model flag do in Codex?"</p>
            <p className="compare-a">
              The --model flag (short form -m){" "}
              <span className="highlight-phrase">
                sets which model Codex uses for that run
              </span>
              <sup className="citation-chip">1</sup>, overriding your configured
              default. It applies only to the current invocation.
            </p>
            <div className="source-card-inline">
              <span className="source-number">1</span>
              <div className="source-info">
                <div className="source-title">Codex CLI — Command reference</div>
                <div className="source-url">developers.openai.com/codex/cli/reference</div>
              </div>
            </div>
            <div className="compare-footer good">
              <span>✓</span>
              One claim, one source — open it and check for yourself.
            </div>
          </div>
        </div>
      </div>

      {/* Stats row */}
      <div className="stats-row">
        <div className="stat-cell">
          <div className="stat-value">≥95%</div>
          <div className="stat-label">citation correctness</div>
        </div>
        <div className="stat-cell">
          <div className="stat-value">100%</div>
          <div className="stat-label">guardrail</div>
        </div>
        <div className="stat-cell">
          <div className="stat-value">≥95%</div>
          <div className="stat-label">retrieval hit rate</div>
        </div>
      </div>

      {/* Feature cards */}
      <div className="features-row">
        {[
          { mark: "❝", title: "Citation on every claim", body: "No factual sentence without a source you can open and check." },
          { mark: "⦸", title: "Refuses out-of-scope", body: "Ask about anything but the four tools and it declines — cleanly." },
          { mark: "⌗", title: "Exact lookup", body: "Flags, commands, model names, config keys and errors, matched precisely." },
          { mark: "↵", title: "Clean follow-ups", body: "Switch tools mid-session without context bleeding across products." },
        ].map((f, i) => (
          <div key={i} className="feature-card">
            <span className="feature-icon">{f.mark}</span>
            <h3>{f.title}</h3>
            <p>{f.body}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// InteractiveDemo
// ---------------------------------------------------------------------------

export function InteractiveDemo({
  demoQuestions,
  demo,
  onOpenChat,
}: {
  demoQuestions: Array<{
    key: string;
    q: string;
    tag: string;
    active: boolean;
    select: () => void;
    btnStyle: React.CSSProperties;
  }>;
  demo: {
    q: string;
    text: string;
    streaming: boolean;
    done: boolean;
    refusal: boolean;
    showSources: boolean;
    sources: Source[];
  };
  onOpenChat: () => void;
}) {
  return (
    <section id="demo" className="section">
      <div className="demo-wrapper">
        <div className="demo-inner">
          <div className="demo-left">
            <div className="mono-label">Live demo</div>
            <h3>Ask a question.</h3>
            <p>Pick one — watch CiteVyn answer, cite, or refuse.</p>
            <div className="demo-questions">
              {demoQuestions.map((dq) => (
                <button
                  key={dq.key}
                  onClick={dq.select}
                  className={`demo-question demo-q-btn${dq.active ? " active" : ""}`}
                  style={dq.btnStyle}
                >
                  <span>{dq.q}</span>
                  <span className="demo-q-tag">{dq.tag}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="demo-right">
            <div className="demo-question-row">
              <div className="avatar user-avatar">Q</div>
              <p>{demo.q}</p>
            </div>
            <div className="demo-answer-row">
              <div className="bot-avatar">CV</div>
              <div>
                {demo.refusal && (
                  <div className="refusal-badge">
                    ⚠ NO SOURCE — REFUSED
                  </div>
                )}
                <p className="demo-answer">
                  {demo.text}
                  {demo.streaming && <span className="typing-cursor" />}
                </p>

                {demo.showSources && demo.sources.length > 0 && (
                  <div className="sources">
                    {demo.sources.map((src) => (
                      <div key={src.n} className="source-card">
                        <span className="source-number">{src.n}</span>
                        <div className="source-info">
                          <div className="source-title">{src.title}</div>
                          <div className="source-url">{src.url}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {demo.done && (
                  <button onClick={onOpenChat} className="continue-btn">
                    Continue in full chat →
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Pricing
// ---------------------------------------------------------------------------

// `action` names the CTA behavior as data, so the JSX doesn't branch on the
// tier name: "openChat"/"getPro" resolve to a handler below; null = inert
// (a true no-op, rendered disabled and out of the tab order).
type TierAction = "openChat" | "getPro" | null;

const tierDefs: Array<{
  name: string;
  price: string;
  unit: string;
  desc: string;
  cta: string;
  featured: boolean;
  action: TierAction;
  features: string[];
}> = [
  {
    name: "Demo",
    price: "$0",
    unit: "/forever",
    desc: "Try citation-backed answers across all four tools.",
    cta: "Start asking",
    featured: false,
    action: "openChat",
    features: [
      "All four tools: Claude, Claude Code, Codex, Gemini",
      "Citations on every factual answer",
      "Out-of-scope refusal guardrail",
      "Bounded follow-up sessions",
    ],
  },
  {
    name: "Pro",
    price: "$12",
    unit: "/month",
    desc: "For developers who live in the docs.",
    cta: "Get Pro",
    featured: true,
    action: "getPro",
    features: [
      "Everything in Demo",
      "Higher rate limits & faster models",
      "Exact lookup for flags, commands & config keys",
      "Saved history & shareable answers",
      "Retrieval & cost observability",
    ],
  },
  {
    name: "Enterprise",
    price: "Custom",
    unit: "",
    desc: "For teams with private docs and governance needs.",
    cta: "Contact sales",
    featured: false,
    action: null,
    features: [
      "Private documentation connectors",
      "More sources (ChatGPT, Cursor) & scheduled refresh",
      "SSO, RBAC & tenant isolation",
      "Audit exports & compliance controls",
      "Slack & Teams integrations",
    ],
  },
];

export function Pricing({
  onGetPro,
  onOpenChat,
}: {
  onGetPro: () => void;
  onOpenChat: () => void;
}) {
  const handlers: Record<Exclude<TierAction, null>, () => void> = {
    openChat: onOpenChat,
    getPro: onGetPro,
  };
  return (
    <section id="pricing" className="section">
      <div className="section-header centered">
        <span className="mono-label">Pricing</span>
        <h2>Start free. Scale when you trust it.</h2>
        <p>Every tier answers only from official docs, with citations.</p>
      </div>
      <div className="pricing-grid">
        {tierDefs.map((tier, i) => (
          <div
            key={i}
            className={`pricing-card ${tier.featured ? "featured" : ""}`}
            style={{
              borderColor: tier.featured
                ? "var(--ink)"
                : "var(--border)",
              boxShadow: tier.featured
                ? "0 24px 50px -30px rgba(0,0,0,0.35)"
                : "0 1px 2px rgba(0,0,0,0.03)",
            }}
          >
            {tier.featured && (
              <>
                <div className="popular-bar" />
                <span className="popular-badge">POPULAR</span>
              </>
            )}
            <h3>{tier.name}</h3>
            <div className="price-row">
              <span className="price">{tier.price}</span>
              <span className="unit">{tier.unit}</span>
            </div>
            <p>{tier.desc}</p>
            <button
              onClick={tier.action ? handlers[tier.action] : undefined}
              disabled={!tier.action}
              className={`cta ${tier.featured ? "cta-filled" : "cta-outlined"}`}
            >
              {tier.cta}
            </button>
            <ul className="feature-list">
              {tier.features.map((feat, j) => (
                <li key={j}>
                  <span className="check">✓</span> {feat}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// FAQ
// ---------------------------------------------------------------------------

const faqDefs = [
  {
    q: "Which tools does CiteVyn cover?",
    a: "The MVP covers Claude (API), Claude Code, OpenAI Codex, and Google Gemini — using their official documentation only. ChatGPT and Cursor are on the roadmap, not in the MVP.",
  },
  {
    q: "How do citations work?",
    a: "Every factual answer is generated only from retrieved documentation chunks, and each is attached to the exact source page it came from. If a claim isn't supported by a source, it isn't made.",
  },
  {
    q: "What happens when it can't find an answer?",
    a: "CiteVyn refuses rather than guesses. If the docs don't support a reliable answer, or the question is outside the supported tools, it tells you so plainly instead of hallucinating.",
  },
  {
    q: "Does CiteVyn hallucinate?",
    a: "It's designed not to. Answers are grounded in indexed official docs and gated by an evaluation suite targeting 95%+ citation correctness and faithfulness before release.",
  },
  {
    q: "Can it answer questions about my private docs?",
    a: "Not in the MVP — it uses public official documentation only. Private-source ingestion, SSO, and tenant isolation are part of the Enterprise roadmap.",
  },
  {
    q: "How fresh is the documentation?",
    a: "CiteVyn serves from the last known-good index, so a failed re-index never corrupts what's live. Scheduled source refresh is an Enterprise feature.",
  },
];

export function FAQ({
  openFaq,
  toggleFaq,
}: {
  openFaq: number;
  toggleFaq: (i: number) => void;
}) {
  return (
    <section id="faq" className="section">
      <div className="section-header centered">
        <span className="mono-label">FAQ</span>
        <h2>Questions, answered.</h2>
      </div>
      <div className="faq-list">
        {faqDefs.map((item, i) => (
          <div key={i} className="faq-item">
            <button
              onClick={() => toggleFaq(i)}
              className="faq-toggle"
              id={`faq-toggle-${i}`}
              aria-expanded={openFaq === i}
              aria-controls={`faq-answer-${i}`}
            >
              <span>{item.q}</span>
              <span className="faq-sign">{openFaq === i ? "−" : "+"}</span>
            </button>
            {openFaq === i && (
              <p className="faq-answer" id={`faq-answer-${i}`}>
                {item.a}
              </p>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// CTABanner
// ---------------------------------------------------------------------------

export function CTABanner({
  onOpenChat,
}: {
  onOpenChat: () => void;
}) {
  return (
    <section>
      <div className="cta-banner">
        <h2>
          Stop guessing. Start <span className="highlight">citing.</span>
        </h2>
        <p>
          Ask CiteVyn anything about Claude, Claude Code, Codex, and Gemini.
        </p>
        <button onClick={onOpenChat} className="cta-pill">
          Ask your first question →
        </button>
        <p className="cta-footnote">NO ACCOUNT · NO SETUP · FIRST ANSWER IN SECONDS</p>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Footer
// ---------------------------------------------------------------------------

export function Footer() {
  return (
    <footer>
      <div className="footer-inner">
        <div className="footer-logo">
          <span>CiteVyn</span>
          <sup className="logo-badge">01</sup>
        </div>
        <div className="footer-meta">
          <span className="mvp-badge">MVP</span>
          <span>Claude · Claude Code · Codex · Gemini</span>
        </div>
        <div className="footer-copy">© 2026 CiteVyn. Answers from official docs only.</div>
      </div>
    </footer>
  );
}