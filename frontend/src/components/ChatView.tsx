/**
 * Main chat surface.
 *
 * Owns:
 *   - the local message list (user + assistant turns in a session)
 *   - the in-flight state of the latest request
 *   - the lazy session creation (first message triggers a POST
 *     /v1/sessions; subsequent ones reuse the cached id)
 *
 * The component is *stateful* (it owns the message list) but
 * *not* the session id — that lives in the parent (App.tsx) and
 * is plumbed in via props so a "New session" button can reset it.
 *
 * Errors are caught here and rendered as inline assistant
 * bubbles with a retry button; rate-limit (429) is escalated to
 * the parent so the toast can announce it.
 *
 * The chat surface sits on the landing page (see App.tsx and
 * LandingView.tsx); the panel below owns its own scroll so the
 * landing sections above and below remain anchored.
 */

import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import { askQuestion, createSession } from "../lib/api";
import type { AskResponse, SessionId } from "../lib/types";
import { ApiClientError } from "../lib/types";

import { ExamplePrompts } from "./ExamplePrompts";
import {
  AssistantMessage,
  UserMessage,
  type AssistantMessageState,
} from "./MessageBubble";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ChatTurn {
  /** Stable id, used as the React key. */
  id: string;
  question: string;
  /** The most recent in-flight state for this turn. */
  state: AssistantMessageState;
  /** The request id, when known, used to display the trace marker. */
  requestId?: string;
}

export interface ChatViewProps {
  sessionId: SessionId | null;
  sessionStartedAt: string | null;
  messageCount: number;
  indexVersion: string | null;
  answerPolicyVersion: string | null;
  onSessionCreated: (id: SessionId) => void;
  onError: (error: ApiClientError) => void;
  /** Surfaced to the parent so the landing footer can show "policy v1". */
  onResponseMetadata?: (response: AskResponse) => void;
  onNewSession: () => void;
  onSwitchView: (view: "chat" | "exact" | "about") => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Maximum number of turns kept in memory. Old turns are
 * truncated from the head of the array; the backend already
 * has them via the session, but the UI only needs the recent
 * window to look alive. ``20`` is enough for a demo, low
 * enough to keep the DOM snappy.
 */
const MAX_TURNS = 20;

export function ChatView({
  sessionId,
  messageCount,
  onSessionCreated,
  onError,
  onResponseMetadata,
  onNewSession,
}: ChatViewProps) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  // The latest question whose response is still in flight —
  // rendered as a typing indicator after the user's bubble.
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const formRef = useRef<HTMLFormElement | null>(null);
  const turnIdRef = useRef(0);

  // Listen for landing-page prompt requests (the hero waitlist form
  // dispatches a custom event the chat view listens for).
  useEffect(() => {
    const onPromptEvent = (event: Event) => {
      const custom = event as CustomEvent<string>;
      if (typeof custom.detail === "string" && custom.detail.trim()) {
        setInput(custom.detail);
        // Focus the textarea on the next tick so the user can edit.
        window.setTimeout(() => {
          formRef.current?.querySelector("textarea")?.focus();
        }, 0);
      }
    };
    window.addEventListener("citevyn:set-prompt", onPromptEvent);
    return () => {
      window.removeEventListener("citevyn:set-prompt", onPromptEvent);
    };
  }, []);

  // Auto-scroll to the latest turn whenever the list changes.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [turns, pendingQuestion]);

  // ---------------------------------------------------------------------
  // Submit
  // ---------------------------------------------------------------------

  const handleSubmit = async (rawQuestion: string) => {
    const question = rawQuestion.trim();
    if (!question || busy) return;

    setInput("");
    setBusy(true);
    setPendingQuestion(question);

    const turnId = `turn-${++turnIdRef.current}`;

    try {
      // Lazy session creation. If the parent hasn't given us a
      // session id yet, create one before asking the question.
      // If the request fails partway, the in-flight state
      // below is replaced with the error message — the session
      // id is still set for the next attempt.
      let activeSession = sessionId;
      if (!activeSession) {
        const session = await createSession();
        activeSession = session.session_id;
        onSessionCreated(session.session_id);
      }

      const response = await askQuestion(activeSession, question);
      const state: AssistantMessageState = response.unsupported || response.no_answer
        ? {
            kind: "refused",
            response,
            reason: response.unsupported ? "unsupported" : "no_answer",
          }
        : { kind: "grounded", response };

      setTurns((prev) => {
        const next = [...prev, { id: turnId, question, state, requestId: response.request_id }];
        return next.length > MAX_TURNS ? next.slice(-MAX_TURNS) : next;
      });
      if (onResponseMetadata) onResponseMetadata(response);
    } catch (err) {
      const apiError =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(
              err instanceof Error ? err.message : String(err),
              0,
              String(err),
            );
      onError(apiError);
      setTurns((prev) => {
        const next = [
          ...prev,
          {
            id: turnId,
            question,
            state: {
              kind: "error" as const,
              message: apiError.message,
              onRetry: () => handleSubmit(question),
            },
          },
        ];
        return next.length > MAX_TURNS ? next.slice(-MAX_TURNS) : next;
      });
    } finally {
      setBusy(false);
      setPendingQuestion(null);
    }
  };

  // ---------------------------------------------------------------------
  // Form wiring
  // ---------------------------------------------------------------------

  const onFormSubmit = (e: FormEvent) => {
    e.preventDefault();
    void handleSubmit(input);
  };

  const onTextareaKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter submits; Shift+Enter inserts a newline. The native
    // textarea behaviour already handles Shift+Enter so we only
    // need to intercept the bare Enter.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void handleSubmit(input);
    }
  };

  // ---------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------

  const showHero = turns.length === 0 && !pendingQuestion;

  return (
    <section className="chat-panel" aria-label="Cited RAG chat">
      <header className="chat-panel__header">
        <div className="chat-panel__session-info">
          <span className="chat-panel__session-badge">
            <span className="chat-panel__session-badge--dot" />
            AI Powered
          </span>
          <span className="chat-panel__eyebrow">ASK CITEVYN</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-4)" }}>
          <h2 className="chat-panel__title">
            Answers grounded in <span className="landing__highlight landing__highlight--inline">
              <span className="landing__highlight-bar" aria-hidden="true" />
              <span className="landing__highlight-text">official docs</span>
            </span>
          </h2>
          {messageCount > 0 && (
            <button
              type="button"
              className="button button--secondary button--small"
              onClick={onNewSession}
              data-testid="btn-new-session"
            >
              + New session
            </button>
          )}
        </div>
      </header>

      <div className="chat-panel__scroll" ref={scrollRef} role="log" aria-live="polite" aria-relevant="additions">
        {showHero ? (
          <ExamplePrompts
            onSelect={(p) => {
              setInput(p);
              formRef.current?.querySelector("textarea")?.focus();
            }}
          />
        ) : (
          <div className="chat-thread">
            {turns.map((turn) => (
              <div key={turn.id} className="chat-turn">
                <UserMessage text={turn.question} />
                <AssistantMessage state={turn.state} />
              </div>
            ))}
            {pendingQuestion && (
              <div className="chat-turn">
                <UserMessage text={pendingQuestion} />
                <AssistantMessage state={{ kind: "in-flight" }} />
              </div>
            )}
          </div>
        )}
      </div>

      <form ref={formRef} className="composer" onSubmit={onFormSubmit}>
        <div className="composer__form">
          <textarea
            className="composer__textarea"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onTextareaKey}
            placeholder={busy ? "Generating answer…" : "Ask a question about Claude, Claude Code, Codex, or Gemini…"}
            rows={1}
            disabled={busy}
            aria-label="Ask a question about Claude, Claude Code, Codex, or Gemini"
            data-testid="chat-input"
          />
          <button
            type="submit"
            className="composer__send"
            disabled={busy || input.trim().length === 0}
            aria-label={busy ? "Sending question" : "Send question"}
            data-testid="chat-send-btn"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        </div>
        <small className="composer__hint">
          <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for a new line
        </small>
      </form>
    </section>
  );
}