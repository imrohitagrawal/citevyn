/**
 * Hero — Two-column hero section with input + auto-playing answer card.
 */

import type * as React from "react";
import { EMPTY_SUBMIT_NUDGE_GLYPH } from "../lib/composerNudge";

interface HeroProps {
  heroInput: string;
  heroPlaceholder: string;
  /** #446 review: the MESSAGE to show, or null. Was a boolean with the sentence
      hard-coded below; two refusals share this mechanism now. */
  heroNudge: string | null;
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
            Cited answers for your AI tools
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
            {/* `--muted`, not `--faint` (#396). An INLINE style beats every
             * stylesheet rule, so the CSS-only sweeps of this token missed
             * this glyph and the `TRY:` label below twice over.
             *
             * The `▸` is the one place with a real claim to WCAG 1.4.3's
             * "pure decoration" exemption — a single ornamental glyph, no
             * words, no functionality — and that argument is recorded rather
             * than pretended away. Declined: `--faint` measures 2.92:1 at BEST
             * in the light theme, under even the 3:1 large-text floor, so it
             * can colour no text at any size, and a live consumer keeps the
             * trap loaded for whoever copies this block next. */}
            <span
              style={{
                fontFamily: "'JetBrains Mono', monospace",
                color: "var(--muted)",
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

          {/* `aria-hidden` (#445 review): this is a VERBATIM duplicate of the
              live region below, so without it a reader browsing the page meets
              the same sentence twice — once as static text here and once
              announced there. Hiding the visual copy costs a screen-reader user
              nothing, because the region carries the identical string. */}
          {heroNudge && (
            <p className="hero-nudge" aria-hidden="true">
              {EMPTY_SUBMIT_NUDGE_GLYPH} {heroNudge}
            </p>
          )}
          {/* #445. ALWAYS rendered, empty when idle — the same shape, and for
              the same reason, as the region beside the chat composer: a live
              region has to be in the accessibility tree BEFORE its text
              changes, and the visible `.hero-nudge` above is created together
              with its own text, which is the classic way to make an
              announcement not fire. That is why the "correct" composer was not
              announced either, and why copying it verbatim into the chat would
              have copied a silent fix.

              The `⚠` stays OUT of this string. It is decoration for the eye;
              inside a live region it is one more thing read aloud before the
              sentence that matters.

              `.sr-only` is declared once, unscoped, but filed under
              `landing.css`'s "16. CHAT VIEW" section. The hero depends on it
              from here, which nothing in that file says. Scope it to
              `.composer` some day and this sentence renders VISIBLY, twice,
              with nothing going red — the nudge is in no visual baseline. */}
          <p className="sr-only" role="status">
            {heroNudge ?? ""}
          </p>

          <div className="hero-chips">
            {/* `--muted`, not `--faint` (#396). Real WORDS at 11px — small
             * text needing 4.5:1, which `--faint` misses in both themes
             * (2.77:1 light on `--bg`, 3.49:1 dark). No decoration argument
             * applies here at all. Inline, so no CSS sweep could see it. */}
            <span
              style={{
                fontFamily: "'JetBrains Mono', monospace",
                fontSize: "11px",
                color: "var(--muted)",
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