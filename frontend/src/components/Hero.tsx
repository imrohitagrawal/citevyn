/**
 * Hero — Two-column hero section with input + auto-playing answer card.
 */

import type * as React from "react";

interface HeroProps {
  heroInput: string;
  heroPlaceholder: string;
  heroNudge: boolean;
  heroBoxShake: boolean;
  heroRef: React.RefObject<HTMLInputElement>;
  onHeroInput: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onHeroKey: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onAskHero: () => void;
  onFocusHero: () => void;
  heroChips: Array<{ q: string; select: () => void }>;
  hero: {
    q: string;
    text: string;
    streaming: boolean;
    showSources: boolean;
    sources: Array<{ n: string; title: string; url: string }>;
  };
  heroDots: Array<{ active: boolean; style: React.CSSProperties }>;
}

export function Hero({
  heroInput,
  heroPlaceholder,
  heroNudge,
  heroBoxShake,
  heroRef,
  onHeroInput,
  onHeroKey,
  onAskHero,
  onFocusHero,
  heroChips,
  hero,
  heroDots,
}: HeroProps) {
  return (
    <section className="hero">
      <div className="hero-container">
        {/* Left column */}
        <div>
          <div className="status-badge">
            <span className="status-dot" />
            Cited answers for AI dev tools
          </div>

          <h1 className="hero-title">
            Answers you can <span className="highlight">check,</span> not just
            believe.
          </h1>

          <p className="hero-description">
            Ask anything about Claude, Claude&nbsp;Code, Codex, or Gemini.
            CiteVyn answers{" "}
            <em
              style={{
                fontFamily: "'Newsreader', serif",
                fontStyle: "italic",
                color: "var(--ink)",
              }}
            >
              straight from the makers' own guides
            </em>{" "}
            — links every claim to the exact page it came from, and says "I
            don't know" instead of guessing.
          </p>

          <div
            className={`hero-input-box${heroBoxShake ? " shake" : ""}`}
            style={{
              borderColor: heroNudge ? "var(--refusal-amber)" : undefined,
            }}
          >
            <span
              style={{
                fontFamily: "'JetBrains Mono', monospace",
                color: "var(--faint)",
                fontSize: "15px",
              }}
            >
              ▸
            </span>
            <input
              ref={heroRef}
              id="hero-input"
              value={heroInput}
              onChange={onHeroInput}
              onKeyDown={onHeroKey}
              placeholder={heroPlaceholder}
              style={{
                flex: 1,
                border: "none",
                outline: "none",
                background: "transparent",
                fontFamily: "inherit",
                fontSize: "16px",
                color: "var(--ink)",
                minWidth: 0,
              }}
            />
            <button
              onClick={onFocusHero}
              title="Press / anywhere to focus this box"
              className="shortcut-badge"
            >
              /
            </button>
            <button onClick={onAskHero} className="ask-button">
              Ask →
            </button>
          </div>

          {heroNudge && (
            <p className="hero-nudge">
              ⚠ Type a question first — or tap one of the examples below.
            </p>
          )}

          <div className="hero-chips">
            <span
              style={{
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: "11px",
                color: "var(--faint)",
                alignSelf: "center",
                marginRight: "2px",
              }}
            >
              TRY:
            </span>
            {heroChips.map((chip, i) => (
              <button
                key={i}
                onClick={() => chip.select()}
                className="hero-chip"
              >
                {chip.q}
              </button>
            ))}
          </div>
        </div>

        {/* Right column - Hero Answer Card */}
        <div className="hero-card">
          {/* macOS window chrome */}
          <div className="card-header">
            <span className="traffic-dot red-dot" />
            <span className="traffic-dot yellow-dot" />
            <span className="traffic-dot green-dot" />
            <span className="card-title">CiteVyn — live session</span>
            <span className="auto-badge">
              <span className="auto-dot" />
              AUTO
            </span>
          </div>

          <div className="card-content">
            {/* Question */}
            <div className="message">
              <div className="avatar user-avatar">Q</div>
              <p className="message-text">{hero.q}</p>
            </div>

            {/* Answer */}
            <div className="message" style={{ flex: 1 }}>
              <div className="bot-avatar">CV</div>
              <div style={{ minWidth: 0, flex: 1 }}>
                <p
                  className="bot-message"
                  style={{ minHeight: "92px" }}
                >
                  {hero.text}
                  {hero.streaming && (
                    <span className="typing-cursor" />
                  )}
                </p>

                {hero.showSources && hero.sources.length > 0 && (
                  <div className="sources">
                    {hero.sources.map((src) => (
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
              </div>
            </div>

            {/* Progress dots */}
            <div className="progress-container">
              <div className="progress-dots">
                {heroDots.map((dot, i) => (
                  <span key={i} className={`progress-dot${dot.active ? " active" : ""}`} style={dot.style} />
                ))}
              </div>
              <span className="progress-text">
                answers stream in real time
              </span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}