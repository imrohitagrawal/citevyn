/**
 * Citation Terminal landing shell (option 4 of 4).
 *
 * Always-dark, mono-everything terminal aesthetic. The page reads as a
 * live `citevyn ask` session — same fixes applied as DevToolsApp:
 *   - No fake browser tabs.
 *   - Chunk cards are interactive (click to expand).
 *   - Citation chips jump to the matching chunk.
 *   - FAQ is folded into the README, not a separate section.
 *   - Hero stats describe product guarantees, not infrastructure uptime.
 */

import { useEffect, useState } from "react";

import type { AskResponse, SessionId } from "../lib/types";
import { ApiClientError } from "../lib/types";
import { ChatView } from "./ChatView";

interface TerminalAppProps {
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

// ---------------------------------------------------------------------------
// Terminal chrome — title bar with traffic lights + a single session line
// ---------------------------------------------------------------------------

function TerminalChrome() {
  return (
    <header className="dt__chrome" role="banner">
      <div className="dt__chrome-row">
        <div className="dt__traffic-lights" aria-hidden="true">
          <span className="dt__traffic-light dt__traffic-light--red" />
          <span className="dt__traffic-light dt__traffic-light--yellow" />
          <span className="dt__traffic-light dt__traffic-light--green" />
        </div>
        <div className="dt__chrome-url">
          <span className="dt__chrome-url-method">ASK</span>
          <span className="dt__chrome-url-path">citevyn.ask — session dev-2024</span>
          <span className="dt__chrome-url-status">12 msgs · 3 chunks · 142ms</span>
        </div>
        <span className="dt__chrome-pill">
          <span className="dt__chrome-pill-dot" aria-hidden="true" />
          live
        </span>
      </div>
    </header>
  );
}

// ---------------------------------------------------------------------------
// Hero — terminal trace (live-looking citation session)
// ---------------------------------------------------------------------------

interface SampleChunk {
  id: string;
  source: string;
  span: string;
  score: number;
  text: string;
}

const SAMPLE_CHUNKS: ReadonlyArray<SampleChunk> = [
  {
    id: "chunk-01",
    source: "docs/rag/architecture.md",
    span: "§2.4:14–§2.4:38",
    score: 0.94,
    text: "Chunking splits the corpus into 200–400 token windows with a 20-token overlap. Each window keeps its source path and byte span so the model can cite it back.",
  },
  {
    id: "chunk-02",
    source: "docs/rag/scoring.md",
    span: "§1.1:03–§1.1:22",
    score: 0.87,
    text: "Cosine similarity is computed against the query embedding. Scores above 0.6 require the model to attach a citation; below 0.6 the answer is marked unverified.",
  },
  {
    id: "chunk-03",
    source: "threads/slack-#eng/2024-11-08",
    span: "msg #4421",
    score: 0.71,
    text: "We picked chunk-01 as the canonical reference because §2.4 explains the overlap rule in plain language. Engineers reading the doc should land there first.",
  },
];

function HeroSplit() {
  const [activeChunk, setActiveChunk] = useState<string>("chunk-01");

  return (
    <section className="dt__hero" aria-labelledby="dt-hero-headline">
      <div>
        <div className="dt__hero-eyebrow">
          <span className="dt__hero-eyebrow-dot" aria-hidden="true" />
          <span>citation trace v2 · live</span>
        </div>
        <h1 id="dt-hero-headline" className="dt__hero-headline">
          cited answers for <span className="dt__hero-headline-accent">ai dev tools.</span>
        </h1>
        <p className="dt__hero-sub">
          CiteVyn is a RAG assistant for Claude, Claude Code, Codex, and Gemini that
          shows you the exact chunks it cited — score, source path, and span —
          every single time. No more "the model said so."
        </p>
        <div className="dt__hero-ctas">
          <a
            href="#dt-demo"
            className="dt__btn dt__btn--primary"
            onClick={(e) => {
              e.preventDefault();
              document.getElementById("dt-demo")?.scrollIntoView({ behavior: "smooth", block: "start" });
            }}
          >
            Try the demo
            <span aria-hidden="true">→</span>
          </a>
          <a
            href="#dt-readme"
            className="dt__btn dt__btn--secondary"
            onClick={(e) => {
              e.preventDefault();
              document.getElementById("dt-readme")?.scrollIntoView({ behavior: "smooth", block: "start" });
            }}
          >
            Read the docs
          </a>
        </div>
        <dl className="dt__hero-stats" aria-label="Product guarantees">
          <div>
            <dt>citation</dt>
            <dd>required above 0.6 confidence</dd>
          </div>
          <div>
            <dt>providers</dt>
            <dd>Claude, Claude Code, Codex, Gemini</dd>
          </div>
          <div>
            <dt>data</dt>
            <dd>public docs only — nothing logged</dd>
          </div>
        </dl>
      </div>

      <div className="dt__inspector" aria-label="Citation trace">
        <div className="dt__inspector-chrome">
          <div className="dt__inspector-dots" aria-hidden="true">
            <span /><span /><span />
          </div>
          <span className="dt__inspector-title">citation trace</span>
          <span className="dt__inspector-meta">3 chunks · 142ms</span>
        </div>

        <div className="dt__inspector-stage">
          <div className="dt__inspector-chunks" role="tablist" aria-label="Retrieved chunks">
            {SAMPLE_CHUNKS.map((c, i) => {
              const isActive = c.id === activeChunk;
              return (
                <button
                  key={c.id}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  className={
                    "dt__inspector-card" + (isActive ? " dt__inspector-card--active" : "")
                  }
                  onClick={() => setActiveChunk(c.id)}
                >
                  <span className="dt__inspector-card-head">
                    <span className="dt__inspector-card-tag">[{i + 1}]</span>
                    <span className="dt__inspector-card-id">{c.id}</span>
                    <span className="dt__inspector-card-score">score {c.score.toFixed(2)}</span>
                  </span>
                  <span className="dt__inspector-card-source">{c.source}</span>
                  {isActive && (
                    <span className="dt__inspector-card-body">{c.text}</span>
                  )}
                  {isActive && (
                    <span className="dt__inspector-card-span">span: {c.span}</span>
                  )}
                </button>
              );
            })}
          </div>

          <div className="dt__inspector-answer" aria-label="Sample answer with citations">
            <div className="dt__inspector-answer-label">assistant</div>
            <p className="dt__inspector-answer-text">
              CiteVyn chunks the corpus into 200–400 token windows with a 20-token overlap
              {" "}
              <button type="button" className="dt__cite" onClick={() => setActiveChunk("chunk-01")}>[1]</button>.
              Each chunk gets a cosine-similarity score against the query
              {" "}
              <button type="button" className="dt__cite" onClick={() => setActiveChunk("chunk-02")}>[2]</button>.
              Above 0.6 the model is required to attach a citation; below that the
              answer is marked unverified
              {" "}
              <button type="button" className="dt__cite" onClick={() => setActiveChunk("chunk-03")}>[3]</button>.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Changelog
// ---------------------------------------------------------------------------

const RELEASE_NOTES = [
  { tag: "v0.9.0", text: "Citation trace v2 — every chunk now carries its score, source path, and byte-level span." },
  { tag: "v0.8.2", text: "Rate-limit policy tuned to 30 req/hour; new 429 envelope with retry-after." },
  { tag: "v0.8.0", text: "Exact-search endpoint exposes ranked snippets alongside the chat answer." },
  { tag: "v0.7.0", text: "Sessions persist across reloads; new /api/v1/sessions and /api/v1/sessions/{id} routes." },
  { tag: "v0.6.0", text: "Answer policy v1.4 — citations are mandatory above 0.6 confidence." },
];

function ReleaseNotes() {
  return (
    <section className="dt__notes" aria-labelledby="dt-notes-eyebrow">
      <h2 id="dt-notes-eyebrow" className="dt__notes-eyebrow">changelog</h2>
      <ul className="dt__notes-list">
        {RELEASE_NOTES.map((n) => (
          <li key={n.tag}>
            <span className="dt__notes-bullet" aria-hidden="true">+</span>
            <span>{n.text}</span>
            <span className="dt__notes-tag">[{n.tag}]</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Interactive workspace — terminal-themed, but functional
// ---------------------------------------------------------------------------

interface WorkspaceFile {
  id: string;
  label: string;
  icon: "file" | "config" | "db";
  body: string;
}

const WORKSPACE_FILES: ReadonlyArray<WorkspaceFile> = [
  { id: "rag", label: "rag.index.ts", icon: "file", body: "export const index = await buildIndex({ sources: corpus, chunkSize: 320, overlap: 20 });" },
  { id: "citations", label: "citations.py", icon: "file", body: "def cite(chunk): return f\"[{chunk.id}] {chunk.source} {chunk.span}\"" },
  { id: "policy", label: "answer-policy.yml", icon: "config", body: "min_citation_score: 0.6\nrequire_citation_above: 0.6\nfallback: 'unverified'" },
  { id: "sessions", label: "sessions.db", icon: "db", body: "dev-2024 · 12 messages · last active 3m ago" },
];

const SHORTCUTS = [
  { keys: ["⌘", "K"], label: "open command bar" },
  { keys: ["⌘", "/"], label: "toggle citation trace" },
  { keys: ["⌥", "C"], label: "copy citation" },
  { keys: ["Esc"], label: "cancel request" },
];

type StateKey = "default" | "hover" | "focus" | "active" | "disabled";

function InteractiveWorkspace() {
  const [activeFile, setActiveFile] = useState<string>("rag");
  const [font, setFont] = useState<string>("Inter");
  const [size, setSize] = useState<string>("14");
  const [weight, setWeight] = useState<string>("500");
  const [elementState, setElementState] = useState<StateKey>("default");

  const file = WORKSPACE_FILES.find((f) => f.id === activeFile) ?? WORKSPACE_FILES[0];
  const stageStyle = {
    fontFamily: font === "system-ui" ? "system-ui, sans-serif" : font,
    fontSize: `${size}px`,
    fontWeight: weight,
  };
  const stateClass = `dt__stage-card--state-${elementState}`;

  return (
    <section className="dt__workspace" aria-labelledby="dt-workspace-title">
      <h2 id="dt-workspace-title" className="dt__section-eyebrow">
        <span className="dt__section-eyebrow-dot" aria-hidden="true" />
        workspace · live selection
      </h2>
      <p className="dt__section-lede">
        Pick a source file, tweak the inspector, watch the stage change. The controls
        are real — the stage card reflects them live.
      </p>
      <div className="dt__workspace-shell">
        <aside className="dt__explorer" aria-label="Source files">
          <h3 className="dt__explorer-title">explorer</h3>
          <ul className="dt__explorer-list">
            {WORKSPACE_FILES.map((item) => (
              <li key={item.id}>
                <button
                  type="button"
                  className={
                    "dt__explorer-item" +
                    (activeFile === item.id ? " dt__explorer-item--active" : "")
                  }
                  aria-pressed={activeFile === item.id}
                  onClick={() => setActiveFile(item.id)}
                >
                  <span className="dt__explorer-item-icon" aria-hidden="true">
                    {item.icon === "config" ? <ConfigIcon /> : item.icon === "db" ? <DatabaseIcon /> : <FileIcon />}
                  </span>
                  {item.label}
                </button>
              </li>
            ))}
          </ul>
          <div className="dt__explorer-shortcuts">
            <h4 className="dt__explorer-shortcuts-title">shortcuts</h4>
            <ul className="dt__explorer-shortcuts-list">
              {SHORTCUTS.map((s) => (
                <li key={s.label}>
                  {s.keys.map((k) => (
                    <span className="dt__explorer-kbd" key={k}>{k}</span>
                  ))}
                  {s.label}
                </li>
              ))}
            </ul>
          </div>
        </aside>

        <div className="dt__stage" aria-label={`Stage: ${file.label}`}>
          <div className={`dt__stage-card ${stateClass}`} style={stageStyle} data-state={elementState}>
            <span className="dt__stage-card-filename">{file.label}</span>
            <span className="dt__stage-card-body">{file.body}</span>
          </div>
          <span className="dt__stage-tag">div.card</span>
        </div>

        <aside className="dt__inspector-panel" aria-label="Property inspector">
          <section>
            <h3 className="dt__inspector-section-title">typography</h3>
            <div className="dt__inspector-grid">
              <label htmlFor="dt-insp-font">font</label>
              <select id="dt-insp-font" value={font} onChange={(e) => setFont(e.target.value)}>
                <option value="Inter">Inter</option>
                <option value="JetBrains Mono">JetBrains Mono</option>
                <option value="system-ui">system-ui</option>
              </select>

              <label htmlFor="dt-insp-size">size</label>
              <div className="dt__inspector-resize">
                <input
                  id="dt-insp-size"
                  type="text"
                  value={`${size}px`}
                  onChange={(e) => {
                    const num = e.target.value.replace(/[^0-9]/g, "");
                    if (num) setSize(num);
                  }}
                />
                <span className="dt__inspector-resize-handle" aria-hidden="true" />
              </div>

              <label htmlFor="dt-insp-weight">weight</label>
              <select id="dt-insp-weight" value={weight} onChange={(e) => setWeight(e.target.value)}>
                <option value="400">400</option>
                <option value="500">500</option>
                <option value="600">600</option>
                <option value="700">700</option>
              </select>
            </div>
          </section>

          <section>
            <h3 className="dt__inspector-section-title">state</h3>
            <div className="dt__inspector-toggle" role="group" aria-label="Element state">
              {(["default", "hover", "focus", "active", "disabled"] as StateKey[]).map((s) => (
                <button
                  key={s}
                  type="button"
                  aria-pressed={elementState === s}
                  onClick={() => setElementState(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          </section>
        </aside>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// README manifesto (with FAQ folded in)
// ---------------------------------------------------------------------------

const FAQ_ITEMS = [
  { q: "What models does CiteVyn support?", a: "Claude, Claude Code, Codex, and Gemini. The citation format is identical across providers — scores and spans are normalized at retrieval time so the same UI works for all four." },
  { q: "How are citations scored?", a: "Each retrieved chunk gets a cosine-similarity score against the query. Above 0.6 confidence we require the model to cite; below that we say so explicitly and mark the answer unverified." },
  { q: "Is my data private?", a: "Yes. The demo index is locked to public docs. Sessions are tied to a UUID in your browser's localStorage; nothing is logged server-side beyond a request id we surface for debugging." },
  { q: "What's the rate limit?", a: "30 requests per hour per session in the demo. The envelope includes a retry-after header that the client surfaces in the toast region so you know when to try again." },
  { q: "Can I self-host?", a: "Not yet — the demo is closed. We're working on a self-hostable build; join the waitlist and we'll let you know when it's ready." },
];

function ReadmeManifesto() {
  return (
    <section className="dt__readme" id="dt-readme" aria-labelledby="dt-readme-h1">
      <div className="dt__readme-shell">
        <div className="dt__readme-header">
          <span className="dt__readme-filename">README.md</span>
          <div className="dt__readme-actions" aria-hidden="true">
            <span /><span /><span />
          </div>
        </div>
        <div className="dt__readme-body">
          <h1 id="dt-readme-h1" className="dt__readme-h1">CiteVyn</h1>
          <p className="dt__readme-p">
            <a href="#dt-demo">Cited answers</a> for AI dev tools. Built for engineers who
            need to know <em>exactly</em> where the model got each claim from.
          </p>

          <h3 className="dt__readme-h3">Why</h3>
          <blockquote className="dt__readme-blockquote">
            Every AI answer is a claim. A claim without a citation is a rumour.
          </blockquote>

          <h3 className="dt__readme-h3">Install</h3>
          <pre className="dt__readme-code">
            <span className="dt__readme-code-com"># pick one</span>{"\n"}
            <span className="dt__readme-code-key">$</span> npm i citevyn{"\n"}
            <span className="dt__readme-code-key">$</span> pip install citevyn{"\n"}
            <span className="dt__readme-code-key">$</span> brew install citevyn
          </pre>

          <h3 className="dt__readme-h3">Usage</h3>
          <pre className="dt__readme-code">
            <span className="dt__readme-code-key">import</span> {"{ citevyn }"}{" "}
            <span className="dt__readme-code-key">from</span>{" "}
            <span className="dt__readme-code-str">"citevyn"</span>;{"\n"}
            {"\n"}
            <span className="dt__readme-code-key">const</span> r = {"await"} citevyn.ask({"{"}
            {"\n  "}query: <span className="dt__readme-code-str">"what does claude code cite?"</span>,{"\n  "}session:{" "}
            <span className="dt__readme-code-str">"dev-2024"</span>,{"\n"}
            {"}"});
          </pre>

          <h3 className="dt__readme-h3">FAQ</h3>
          <dl className="dt__readme-faq">
            {FAQ_ITEMS.map((item) => (
              <div key={item.q} className="dt__readme-faq-item">
                <dt className="dt__readme-faq-q">{item.q}</dt>
                <dd className="dt__readme-faq-a">{item.a}</dd>
              </div>
            ))}
          </dl>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Live demo — chat surface, framed minimally
// ---------------------------------------------------------------------------

function LiveDemo(props: TerminalAppProps) {
  return (
    <section className="dt__demo" id="dt-demo" aria-labelledby="dt-demo-title">
      <h2 id="dt-demo-title" className="dt__section-eyebrow">
        <span className="dt__section-eyebrow-dot" aria-hidden="true" />
        live demo · no signup
      </h2>
      <p className="dt__section-lede">
        Type a question. Every answer comes back with citations. Click a citation to
        see the chunk the model read.
      </p>
      <div className="dt__demo-shell">
        <div className="dt__demo-chrome">
          <div className="dt__demo-dots" aria-hidden="true">
            <span /><span /><span />
          </div>
          <span className="dt__demo-chrome-label">live demo</span>
          <span className="dt__demo-chrome-meta">public docs · 30 req/hour</span>
        </div>
        <div className="dt__demo-body">
          <ChatView
            sessionId={props.sessionId}
            sessionStartedAt={props.sessionStartedAt}
            messageCount={props.messageCount}
            indexVersion={props.indexVersion}
            answerPolicyVersion={props.answerPolicyVersion}
            onSessionCreated={props.onSessionCreated}
            onError={props.onError}
            onResponseMetadata={props.onResponseMetadata}
            onNewSession={props.onNewSession}
            onSwitchView={props.onSwitchView}
          />
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Footer
// ---------------------------------------------------------------------------

function TerminalFooter() {
  return (
    <footer className="dt__footer">
      citevyn · {new Date().getFullYear()} · MIT licensed · built for engineers who want receipts
    </footer>
  );
}

// ---------------------------------------------------------------------------
// Main shell
// ---------------------------------------------------------------------------

export function TerminalApp(props: TerminalAppProps) {
  useEffect(() => {
    if (typeof window !== "undefined") {
      window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
    }
  }, []);

  return (
    <div className="dt__shell">
      <a className="dt__skip-link" href="#dt-demo">Skip to chat</a>
      <TerminalChrome />

      <main>
        <HeroSplit />
        <hr className="dt__divider" />
        <ReleaseNotes />
        <hr className="dt__divider" />
        <InteractiveWorkspace />
        <hr className="dt__divider" />
        <ReadmeManifesto />
        <hr className="dt__divider" />
        <LiveDemo {...props} />
        <TerminalFooter />
      </main>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inline icons
// ---------------------------------------------------------------------------

function FileIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}
function ConfigIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}
function DatabaseIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  );
}
