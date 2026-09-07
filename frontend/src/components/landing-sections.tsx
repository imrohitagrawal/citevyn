/**
 * Landing sections that stay in the EAGER bundle (#358).
 *
 * These three render at or just under the fold, so they must be in the DOM on
 * first paint:
 *   - QuestionTicker / SourcesStrip sit immediately below the Hero. A Suspense
 *     boundary there is a visible layout jump on first paint, which is why the
 *     measured "whole strip lazy" variant was rejected.
 *   - InteractiveDemo is the product demo and the target of the header's "Demo"
 *     nav link. It also sits BETWEEN the two lazy DOM runs, so keeping it here
 *     is what lets the deferred content be one module mounted at two points.
 *     Measured cost of keeping it eager: 406 B gzip (see the PR for #358).
 *
 * Everything below it — Personas, HowItWorks, WhyDifferent, Pricing, FAQ,
 * CTABanner, Footer — lives in ./landing-strip.tsx and is fetched when the
 * reader scrolls toward it. Rollup chunks per MODULE, not per export, so the
 * split has to be physical: React.lazy over named exports of one module frees
 * nothing.
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
      {/* The inner wrapper carries the horizontal padding + centered max-width
          (.sources-strip-inner in landing.css); without it the label jams against
          the viewport's left edge and reads as cut off. */}
      <div className="sources-strip-inner">
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
