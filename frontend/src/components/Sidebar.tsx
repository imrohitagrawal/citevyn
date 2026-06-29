/**
 * Sidebar — session history.
 *
 * V1 will show past sessions per user. The slice-9 demo only has a
 * single in-memory session, so this panel renders a single "Active
 * session" row with the session id and the number of messages so
 * the layout already feels right. A "New session" button clears the
 * current session and starts a new one (deletes the cached id from
 * localStorage and lets the chat view lazily create a fresh one on
 * the next message).
 *
 * The "About this session" block at the bottom shows the index
 * version hash and the answer-policy version after the first
 * message lands — a deliberate reminder that this is a cited
 * RAG, not a general LLM.
 */

import type { SessionId } from "../lib/types";
import { shortId, relativeTime } from "../lib/format";

interface SidebarProps {
  sessionId: SessionId | null;
  messageCount: number;
  sessionStartedAt: string | null;
  indexVersion: string | null;
  answerPolicyVersion: string | null;
  onNewSession: () => void;
}

export function Sidebar({
  sessionId,
  messageCount,
  sessionStartedAt,
  indexVersion,
  answerPolicyVersion,
  onNewSession,
}: SidebarProps) {
  const hasSession = sessionId !== null;

  return (
    <aside className="app__sidebar" aria-label="Session history">
      <div className="sidebar__title">Sessions</div>

      <ul className="sidebar__list">
        {hasSession ? (
          <li className="sidebar__item sidebar__item--active" aria-current="true">
            Active session
            <span className="sidebar__item-meta">
              {shortId(sessionId)} · {messageCount} message{messageCount === 1 ? "" : "s"}
            </span>
          </li>
        ) : (
          <li>
            <div className="sidebar__empty">
              No session yet. Your first message will create one.
            </div>
          </li>
        )}
      </ul>

      <hr className="divider" />

      <button
        type="button"
        className="button button--secondary"
        style={{ width: "100%" }}
        onClick={onNewSession}
        disabled={!hasSession}
        data-testid="sidebar-new-session"
      >
        + New session
      </button>

      <hr className="divider" />

      <div className="sidebar__title">About this session</div>
      <div className="card card--inset" style={{ padding: "var(--space-3)" }}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="tiny muted">Started</span>
          <span className="tiny secondary">{sessionStartedAt ? relativeTime(sessionStartedAt) : "—"}</span>
        </div>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="tiny muted">Index version</span>
          <span className="tiny mono secondary">{indexVersion ?? "—"}</span>
        </div>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="tiny muted">Answer policy</span>
          <span className="tiny mono secondary">{answerPolicyVersion ?? "—"}</span>
        </div>
      </div>

      <hr className="divider" />

      <p className="tiny muted" style={{ lineHeight: "var(--leading-normal)" }}>
        CiteVyn is a cited RAG for Claude, Claude Code, Codex, and Gemini.
        Every answer is grounded in indexed official documentation.
      </p>
    </aside>
  );
}
