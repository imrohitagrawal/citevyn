/**
 * UniversalApp — unified application shell combining Core and DevTools features.
 *
 * Structure:
 *   1. Navigation sidebar — session history and controls (from Sidebar.tsx)
 *   2. Main content area with:
 *      - Chat interface (ChatView) with inline citations
 *      - Live trace view — real-time chunk retrieval visualization
 *
 * This shell is fully self-contained and owns its layout:
 *   - Sidebar for session management
 *   - Chat panel with typing indicator and citation markers
 *   - Collapsible live trace showing the retrieval pipeline
 */

import { useState } from "react";

import type { AskResponse, SessionId } from "../lib/types";
import { ApiClientError } from "../lib/types";
import { ChatView } from "./ChatView";
import { LiveCitationTrace, type CitationChunk } from "./LiveCitationTrace";
import { Sidebar } from "./Sidebar";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface UniversalAppProps {
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

// Sample chunks for the live trace demo
const SAMPLE_CHUNKS: ReadonlyArray<CitationChunk> = [
  {
    id: "chunk-01",
    title: "How chunking works",
    source: "docs/chunking#section-2-4",
    span: "Section 2.4, lines 14–38",
    score: 0.94,
    text: "CiteVyn splits the source into 200–400 token windows with a 20-token overlap. Each window keeps a reference to the exact lines it came from, so the answer can point back to them.",
  },
  {
    id: "chunk-02",
    title: "How citations are scored",
    source: "docs/scoring#section-1-1",
    span: "Section 1.1, lines 3–22",
    score: 0.87,
    text: "Each passage is scored against the question. A passage with a relevance score above 60% must be cited; below that, the answer is marked as unverified rather than guessing.",
  },
  {
    id: "chunk-03",
    title: "What happens when no source matches",
    source: "docs/verification#section-3-2",
    span: "Section 3.2, lines 1–15",
    score: 0.71,
    text: "If the indexed source doesn't cover the question, CiteVyn says so explicitly — no invented flags, no hallucinated env vars, no hedge words.",
  },
];

// ---------------------------------------------------------------------------
// Live Trace Panel — shows retrieval pipeline visualization
// ---------------------------------------------------------------------------

interface LiveTracePanelProps {
  chunks: ReadonlyArray<CitationChunk>;
  activeChunk: string;
  onSelectChunk: (id: string) => void;
  isExpanded: boolean;
  onToggle: () => void;
}

function LiveTracePanel({
  chunks,
  activeChunk,
  onSelectChunk,
  isExpanded,
  onToggle,
}: LiveTracePanelProps) {
  return (
    <div className={`uv-trace-panel${isExpanded ? " uv-trace-panel--expanded" : ""}`}>
      <button
        type="button"
        className="uv-trace-panel__header"
        onClick={onToggle}
        aria-expanded={isExpanded}
        aria-controls="trace-panel-content"
      >
        <span className="uv-trace-panel__title">Live Trace</span>
        <span className="uv-trace-panel__meta">{chunks.length} chunks retrieved</span>
        <span className="uv-trace-panel__toggle" aria-hidden="true">
          {isExpanded ? "▼" : "▶"}
        </span>
      </button>
      <div
        id="trace-panel-content"
        className="uv-trace-panel__content"
        aria-hidden={!isExpanded}
      >
        <LiveCitationTrace
          chunks={chunks}
          activeChunk={activeChunk}
          onSelectChunk={onSelectChunk}
          title="Retrieval pipeline"
          meta={`${chunks.length} passages`}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main App Shell
// ---------------------------------------------------------------------------

export function UniversalApp({
  sessionId,
  sessionStartedAt,
  messageCount,
  indexVersion,
  answerPolicyVersion,
  onSessionCreated,
  onError,
  onResponseMetadata,
  onNewSession,
  onSwitchView,
}: UniversalAppProps) {
  // Panel expansion states
  const [tracePanelExpanded, setTracePanelExpanded] = useState(false);

  // Live trace state (using sample chunks for demo)
  const [activeTraceChunk, setActiveTraceChunk] = useState<string>(SAMPLE_CHUNKS[0].id);

  return (
    <div className="uv-app">
      {/* Navigation Sidebar */}
      <Sidebar
        sessionId={sessionId}
        messageCount={messageCount}
        sessionStartedAt={sessionStartedAt}
        indexVersion={indexVersion}
        answerPolicyVersion={answerPolicyVersion}
        onNewSession={onNewSession}
      />

      {/* Main Content Area */}
      <main className="uv-app__main">
        {/* Top Bar */}
        <header className="uv-app__topbar" role="banner">
          <div className="uv-app__topbar-brand">
            <span className="uv-app__topbar-logo">CITEVYN</span>
            <span className="uv-app__topbar-tag">Universal</span>
          </div>
          <nav className="uv-app__topbar-nav" aria-label="View navigation">
            <button
              type="button"
              className="uv-app__nav-btn uv-app__nav-btn--active"
              onClick={() => onSwitchView("chat")}
              aria-current="page"
            >
              Chat
            </button>
            <button
              type="button"
              className="uv-app__nav-btn"
              onClick={() => onSwitchView("exact")}
            >
              Exact Search
            </button>
            <button
              type="button"
              className="uv-app__nav-btn"
              onClick={() => onSwitchView("about")}
            >
              About
            </button>
          </nav>
        </header>

        {/* Chat Interface */}
        <section className="uv-app__chat" aria-label="Chat interface">
          <ChatView
            sessionId={sessionId}
            sessionStartedAt={sessionStartedAt}
            messageCount={messageCount}
            indexVersion={indexVersion}
            answerPolicyVersion={answerPolicyVersion}
            onSessionCreated={onSessionCreated}
            onError={onError}
            onResponseMetadata={onResponseMetadata}
            onNewSession={onNewSession}
            onSwitchView={onSwitchView}
          />
        </section>

        {/* Live Trace Panel */}
        <LiveTracePanel
          chunks={SAMPLE_CHUNKS}
          activeChunk={activeTraceChunk}
          onSelectChunk={setActiveTraceChunk}
          isExpanded={tracePanelExpanded}
          onToggle={() => setTracePanelExpanded(!tracePanelExpanded)}
        />
      </main>
    </div>
  );
}
