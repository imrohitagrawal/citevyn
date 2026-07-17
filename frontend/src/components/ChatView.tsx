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
  /** Monotonic counter bumped by the hook on every NEW question submit. A change
      here force-scrolls the just-asked question into view even if the reader had
      scrolled up — an explicit send must always be followed. */
  sendTick?: number;
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
  sendTick = 0,
}: ChatViewProps) {
  const chatListRef = useRef<HTMLDivElement>(null);
  // Stick-to-bottom LATCH. Armed (true) means "keep pinning to the bottom as new
  // content streams in"; the first time the user scrolls UP it disarms, so streaming
  // chunks stop yanking them back down. It re-arms only when they return to the true
  // bottom. This replaces the old 120px "slack band": that band re-pinned on every
  // streamed token whenever the user was within 120px of the bottom, so an upward
  // scroll of a few px was instantly reversed by the next chunk — the jitter (#122).
  const stickRef = useRef(true);

  // Keep the latch in sync with the user's manual scrolling. A gesture that leaves
  // the true bottom (>8px) disarms; returning to it re-arms. The effect's own
  // programmatic ``scrollTop = scrollHeight`` lands at the bottom, so it keeps the
  // latch armed (correct) rather than fighting itself.
  useEffect(() => {
    const list = chatListRef.current;
    if (!list) return;
    const onScroll = () => {
      const distanceFromBottom = list.scrollHeight - list.scrollTop - list.clientHeight;
      stickRef.current = distanceFromBottom <= 8;
    };
    list.addEventListener("scroll", onScroll, { passive: true });
    return () => list.removeEventListener("scroll", onScroll);
  }, []);

  // Passive updates (new message OR a streamed chunk) pin to the bottom ONLY while the
  // latch is armed, so a reader who scrolled up keeps their position.
  useEffect(() => {
    const list = chatListRef.current;
    if (!list) return;
    if (stickRef.current) {
      list.scrollTop = list.scrollHeight;
    }
  }, [messages]);

  // An EXPLICIT send always brings the new question into view, even from a scrolled-up
  // position, and re-arms the latch so its streaming answer stays followed. Keyed on
  // ``sendTick`` (bumped once per submit) so it never runs on a passive stream token.
  useEffect(() => {
    if (sendTick === 0) return; // initial mount, no send yet
    const list = chatListRef.current;
    if (!list) return;
    stickRef.current = true;
    list.scrollTop = list.scrollHeight;
  }, [sendTick]);

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