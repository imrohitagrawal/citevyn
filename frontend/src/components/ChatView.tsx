/**
 * ChatView — Full-screen demo chat with canned responses.
 */

import { useEffect, useRef } from "react";

/** A doc URL is a safe link only when it is http(s) or a site-relative path; anything
 *  else (e.g. a ``javascript:`` scheme) renders as inert text, not a clickable link. */
function isSafeHref(url: string): boolean {
  return /^https?:\/\//i.test(url) || url.startsWith("/");
}

interface ChatViewProps {
  messages: Array<{
    isUser: boolean;
    domId: string;
    userStyle: React.CSSProperties;
    text: string;
    streaming?: boolean;
    refusal?: boolean;
    /** A transport failure (rate limit / server / network) — distinct from a content
     *  refusal, so it shows a rate-limit / connection notice, not "NO SOURCE — REFUSED" (#120). */
    errorKind?: "rate_limit" | "error";
    hasSources?: boolean;
    sources?: Array<{ n: string; title: string; url: string }>;
    /** Nearest-doc suggestions on a graceful fallback (Phase 4a). */
    docSuggestions?: Array<{ title: string; url: string; product_area: string }>;
  }>;
  chatEmpty: boolean;
  chatSuggestions: Array<{ q: string; select: () => void }>;
  chatInput: string;
  onChatInput: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onChatKey: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onSendClick: () => void;
  onBackClick: () => void;
  /** When true the chat is wired to the real backend, not canned answers. */
  live?: boolean;
  /** True while waiting for the backend's first chunk. Renders a "thinking…"
      loader so the user knows the request is in flight. */
  pending?: boolean;
  /** Index of the message bubble currently highlighted (e.g. by the
      duplicate-question "jump-to-existing" feature). */
  highlightedIndex?: number;
}

export function ChatView({
  messages,
  chatEmpty,
  chatSuggestions,
  chatInput,
  onChatInput,
  onChatKey,
  onSendClick,
  onBackClick,
  live = false,
  pending = false,
  highlightedIndex = -1,
}: ChatViewProps) {
  const chatListRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new messages are added (or a chunk streams in),
  // but ONLY when the user is already at (or near) the bottom — a user who has
  // scrolled up to read older content keeps their position.
  //
  // The at-bottom check is done SYNCHRONOUSLY here, reading the live
  // ``scrollTop``/``scrollHeight`` when the effect runs, rather than trusting a
  // ref updated by a passive scroll listener. That listener fires asynchronously,
  // so during word-by-word streaming (many updates/sec) it could lag this effect,
  // which then read a stale "at bottom = true" and yanked the view back down while
  // the user was scrolling up — the jitter (#122). Reading synchronously removes
  // the race: after new content is appended, ``distanceFromBottom`` equals just the
  // added height for an at-bottom user (≤ slack → pin) but is large for a
  // scrolled-up user (> slack → left alone).
  useEffect(() => {
    const list = chatListRef.current;
    if (!list) return;
    const slack = 120; // a few streamed lines of tolerance
    const distanceFromBottom = list.scrollHeight - list.scrollTop - list.clientHeight;
    if (distanceFromBottom <= slack) {
      list.scrollTop = list.scrollHeight;
    }
  }, [messages]);

  return (
    <main data-screen-label="Chat">
      <div className="chat-header">
        <button onClick={onBackClick} className="back-button">
          ← Back to landing
        </button>
        <span className="demo-badge">
          {live ? "LIVE — backend answers" : "DEMO — canned responses"}
        </span>
      </div>

      <div ref={chatListRef} id="chat-list" className="chat-container">
        {chatEmpty ? (
          // Empty state
          <div className="empty-state">
            <div className="logo-avatar">CV</div>
            <h2>Ask about your AI tools</h2>
            <p>Claude, Claude Code, Codex, and Gemini — answered from official docs, with citations.</p>
            <div className="suggestions">
              {chatSuggestions.map((s, i) => (
                <button key={i} onClick={s.select} className="suggestion-btn">
                  {s.q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          // Messages
          <>
            {messages.map((m, i) => (
              <div
                key={m.domId}
                id={m.domId}
                className={
                  m.isUser
                    ? `message user user-msg${highlightedIndex === i ? " highlighted" : ""}`
                    : `message bot bot-msg${highlightedIndex === i ? " highlighted" : ""}`
                }
                style={m.isUser ? m.userStyle : undefined}
              >
                {!m.isUser && (
                  <div className="avatar bot-avatar">
                    CV
                  </div>
                )}
                <div className="content">
                  {/* A transport failure (429 / server / network) gets its own notice —
                      NOT the content-refusal badge, which means "the corpus had no
                      answer" and must stay reserved for a genuine no_answer (#120). */}
                  {m.errorKind === "rate_limit" && (
                    <div className="notice-badge notice-rate-limit">⏳ RATE LIMIT REACHED</div>
                  )}
                  {m.errorKind === "error" && (
                    <div className="notice-badge notice-error">⚠ CONNECTION ERROR</div>
                  )}
                  {!m.errorKind && m.refusal && (
                    <div className="refusal-badge">
                      ⚠ NO SOURCE — REFUSED
                    </div>
                  )}
                  <div className={m.streaming ? "streaming" : ""}>
                    {m.text}
                    {m.streaming && <span className="typing-cursor" />}
                  </div>
                  {m.hasSources && m.sources && m.sources.length > 0 && (
                    <div className="sources">
                      {m.sources.map((src) => (
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
                  {/* Graceful fallback (Phase 4a): when the answer was declined but the
                      backend found nearby docs, offer them instead of a dead-end refusal.
                      A doc URL is only made clickable when it is a safe http(s)/relative
                      link — a defensive guard so a bad ``javascript:`` URL (were one ever
                      ingested) renders as inert text, not an executable link. */}
                  {m.docSuggestions && m.docSuggestions.length > 0 && (
                    <div className="suggestions" role="group" aria-label="Related documentation">
                      <div className="suggestions-label">You might find these helpful:</div>
                      {m.docSuggestions.map((s) =>
                        isSafeHref(s.url) ? (
                          <a
                            key={s.url + s.title}
                            className="suggestion-card"
                            href={s.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            <span className="suggestion-title">{s.title}</span>
                            <span className="suggestion-url">{s.url}</span>
                          </a>
                        ) : (
                          <div key={s.url + s.title} className="suggestion-card">
                            <span className="suggestion-title">{s.title}</span>
                            <span className="suggestion-url">{s.url}</span>
                          </div>
                        ),
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {/* Loading indicator while the backend is thinking. Renders as
                its own bot bubble so it scrolls naturally with the rest. */}
            {pending && (
              <div className="message bot bot-msg pending-msg" aria-live="polite">
                <div className="avatar bot-avatar">CV</div>
                <div className="content">
                  <div className="pending-bubble" role="status">
                    <span className="pending-dot" />
                    <span className="pending-dot" />
                    <span className="pending-dot" />
                    <span className="pending-label">Searching the docs…</span>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Composer */}
      <div className="composer">
        <div className="composer-box">
          <span className="composer-prompt">›</span>
          <input
            type="text"
            value={chatInput}
            onChange={onChatInput}
            onKeyDown={onChatKey}
            placeholder="Ask about Claude, Codex, Gemini…"
            className="chat-input"
          />
          <button onClick={onSendClick} className="send-button" aria-label="Send">
            ↑
          </button>
        </div>
        <p className="composer-hint">
          CiteVyn answers from indexed official docs.{" "}
          {live ? "Answers come from the live backend." : "This is a demo with canned responses."}
        </p>
      </div>
    </main>
  );
}