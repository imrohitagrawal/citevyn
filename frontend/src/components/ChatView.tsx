/**
 * ChatView — Full-screen demo chat with canned responses.
 */

import { useEffect, useRef } from "react";

interface ChatViewProps {
  messages: Array<{
    isUser: boolean;
    domId: string;
    userStyle: React.CSSProperties;
    text: string;
    streaming?: boolean;
    refusal?: boolean;
    hasSources?: boolean;
    sources?: Array<{ n: string; title: string; url: string }>;
  }>;
  chatEmpty: boolean;
  chatSuggestions: Array<{ q: string; select: () => void }>;
  chatInput: string;
  onChatInput: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onChatKey: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onSendClick: () => void;
  onBackClick: () => void;
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
}: ChatViewProps) {
  const chatListRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new messages are added (or a chunk streams in):
  // this view owns autoscroll — the hook no longer touches #chat-list.
  useEffect(() => {
    if (chatListRef.current) {
      chatListRef.current.scrollTop = chatListRef.current.scrollHeight;
    }
  }, [messages]);

  return (
    <main data-screen-label="Chat">
      <div className="chat-header">
        <button onClick={onBackClick} className="back-button">
          ← Back to landing
        </button>
        <span className="demo-badge">DEMO — canned responses</span>
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
            {messages.map((m) => (
              <div
                key={m.domId}
                id={m.domId}
                className={m.isUser ? "message user user-msg" : "message bot bot-msg"}
                style={m.isUser ? m.userStyle : undefined}
              >
                {!m.isUser && (
                  <div className="avatar bot-avatar">
                    CV
                  </div>
                )}
                <div className="content">
                  {m.refusal && (
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
                </div>
              </div>
            ))}
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
          CiteVyn answers from indexed official docs. This is a demo with canned responses.
        </p>
      </div>
    </main>
  );
}