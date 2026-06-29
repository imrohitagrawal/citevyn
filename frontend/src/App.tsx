/**
 * Root application component.
 *
 * Manages:
 * - Theme (light/dark, respects OS preference until toggled)
 * - Style variant (core, softly, devtools, devtools-alt, universal)
 * - Session state for the RAG API
 * - View routing (chat, exact, about)
 *
 * The style variant is persisted to localStorage so returning
 * visitors keep their preference.
 */

import { useCallback, useEffect, useState } from "react";
import { ApiClientError, type AskResponse, type SessionId } from "./lib/types";
import { StyleDock } from "./components/StyleDock";
import { TopBar } from "./components/TopBar";
import { ChatView } from "./components/ChatView";
import { AboutView } from "./components/AboutView";
import { ExactSearchView } from "./components/ExactSearchView";
import { ToastRegion } from "./components/Toast";
import { useToast } from "./hooks/useToast";
import { LandingView } from "./components/LandingView";
import { SoftlyApp } from "./components/SoftlyApp";
import { DevToolsApp } from "./components/DevToolsApp";
import { TerminalApp } from "./components/TerminalApp";
import { UniversalApp } from "./components/UniversalApp";

export type ViewId = "chat" | "exact" | "about";
export type StyleId = "core" | "softly" | "devtools" | "devtools-alt" | "universal";

interface AppProps {
  /** Override the style via URL param (?style=universal etc.) */
  styleOverride?: StyleId;
}

export function App({ styleOverride }: AppProps) {
  // ── View routing ──────────────────────────────────────────
  const [view, setView] = useState<ViewId>("chat");

  // ── Theme (light / dark) ──────────────────────────────────
  const [theme, setTheme] = useState<"light" | "dark">(() => {
    if (typeof window === "undefined") return "light";
    return (
      (localStorage.getItem("citevyn:theme") as "light" | "dark") ??
      (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    );
  });

  // ── Style variant ──────────────────────────────────────────
  const [styleId, setStyleId] = useState<StyleId>(() => {
    // URL param takes priority
    const params = new URLSearchParams(window.location.search);
    const fromUrl = params.get("style") as StyleId | null;
    if (fromUrl && isStyleId(fromUrl)) return fromUrl;

    // Stored preference
    const stored = localStorage.getItem("citevyn:style") as StyleId | null;
    if (stored && isStyleId(stored)) return stored;

    return "core";
  });

  // ── Session state ──────────────────────────────────────────
  const [sessionId, setSessionId] = useState<SessionId | null>(() => {
    if (typeof window === "undefined") return null;
    return localStorage.getItem("citevyn:session_id") as SessionId | null;
  });
  const [sessionStartedAt, setSessionStartedAt] = useState<string | null>(null);
  const [messageCount, setMessageCount] = useState(0);
  const [indexVersion, setIndexVersion] = useState<string | null>(null);
  const [answerPolicyVersion, setAnswerPolicyVersion] = useState<string | null>(null);

  // ── Toast notifications ─────────────────────────────────────
  const { toasts, addToast, removeToast } = useToast();

  // ── Persist preferences ────────────────────────────────────
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  useEffect(() => {
    document.documentElement.setAttribute("data-style", styleId);
    if (styleId !== "universal") {
      localStorage.setItem("citevyn:style", styleId);
    }
  }, [styleId]);

  // ── Handlers ────────────────────────────────────────────────
  const handleThemeToggle = useCallback(() => {
    setTheme((prev) => {
      const next = prev === "light" ? "dark" : "light";
      localStorage.setItem("citevyn:theme", next);
      return next;
    });
  }, []);

  const handleStyleChange = useCallback((id: StyleId) => {
    setStyleId(id);
    if (id !== "universal") {
      localStorage.setItem("citevyn:style", id);
    }
  }, []);

  const handleSessionCreated = useCallback((id: SessionId) => {
    setSessionId(id);
    setSessionStartedAt(new Date().toISOString());
    localStorage.setItem("citevyn:session_id", id);
  }, []);

  const handleResponseMetadata = useCallback((response: AskResponse) => {
    setMessageCount((n) => n + 1);
    if (response.index_version) setIndexVersion(response.index_version);
    if (response.policy_version) setAnswerPolicyVersion(response.policy_version);
  }, []);

  const handleNewSession = useCallback(() => {
    setSessionId(null);
    setSessionStartedAt(null);
    setMessageCount(0);
    setIndexVersion(null);
    setAnswerPolicyVersion(null);
    localStorage.removeItem("citevyn:session_id");
  }, []);

  const handleError = useCallback(
    (error: ApiClientError) => {
      if (error.status === 429) {
        addToast({
          kind: "warning",
          title: "Rate limited",
          message: "Too many requests. Please wait a moment.",
        });
      } else {
        addToast({
          kind: "error",
          title: "Something went wrong",
          message: error.message || "An unexpected error occurred.",
        });
      }
    },
    [addToast]
  );

  const handleSwitchView = useCallback((v: ViewId) => {
    setView(v);
  }, []);

  // ── Derived state ───────────────────────────────────────────
  const showAlternateShell =
    view === "chat" &&
    (styleId === "softly" ||
      styleId === "devtools" ||
      styleId === "devtools-alt" ||
      styleId === "universal");

  // ── App shell class ─────────────────────────────────────────
  const appClass = [
    "app",
    styleId === "softly" ? "app--softly" : "",
    styleId === "devtools" ? "app--devtools" : "",
    styleId === "devtools-alt" ? "app--devtools-alt" : "",
    styleId === "universal" ? "app--universal" : "",
  ]
    .filter(Boolean)
    .join(" ");

  // ── Render ──────────────────────────────────────────────────
  return (
    <div className={appClass}>
      {/* Toast notifications */}
      <ToastRegion toasts={toasts} onDismiss={removeToast} />

      {/* Alternate shells (Softly, DevTools, Terminal, Universal) */}
      {showAlternateShell && (
        <>
          {styleId === "softly" && (
            <SoftlyApp
              theme={theme}
              onThemeToggle={handleThemeToggle}
              sessionId={sessionId}
              messageCount={messageCount}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={handleSwitchView}
            />
          )}
          {styleId === "devtools" && (
            <DevToolsApp
              theme={theme}
              onThemeToggle={handleThemeToggle}
              sessionId={sessionId}
              messageCount={messageCount}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={handleSwitchView}
            />
          )}
          {styleId === "devtools-alt" && (
            <TerminalApp
              theme={theme}
              onThemeToggle={handleThemeToggle}
              sessionId={sessionId}
              messageCount={messageCount}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={handleSwitchView}
            />
          )}
          {styleId === "universal" && (
            <UniversalApp
              theme={theme}
              onThemeToggle={handleThemeToggle}
              sessionId={sessionId}
              messageCount={messageCount}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={handleSwitchView}
            />
          )}
        </>
      )}

      {/* Core shell (TopBar + view) */}
      {!showAlternateShell && (
        <>
          <TopBar
            view={view}
            theme={theme}
            onThemeToggle={handleThemeToggle}
            onSwitchView={handleSwitchView}
          />

          <main className="app__main">
            {view === "chat" && (
              <LandingView>
                <ChatView
                  sessionId={sessionId}
                  sessionStartedAt={sessionStartedAt}
                  messageCount={messageCount}
                  indexVersion={indexVersion}
                  answerPolicyVersion={answerPolicyVersion}
                  onSessionCreated={handleSessionCreated}
                  onError={handleError}
                  onResponseMetadata={handleResponseMetadata}
                  onNewSession={handleNewSession}
                  onSwitchView={handleSwitchView}
                />
              </LandingView>
            )}

            {view === "about" && <AboutView />}
            {view === "exact" && (
              <ExactSearchView onError={handleError} />
            )}
          </main>
        </>
      )}

      {/* Style dock for alternate shells */}
      {showAlternateShell && (
        <StyleDock
          currentStyle={styleId}
          onStyleChange={handleStyleChange}
          theme={theme}
          onThemeToggle={handleThemeToggle}
        />
      )}
    </div>
  );
}

// ── Helpers ────────────────────────────────────────────────────
function isStyleId(value: string): value is StyleId {
  return (
    value === "core" ||
    value === "softly" ||
    value === "devtools" ||
    value === "devtools-alt" ||
    value === "universal"
  );
}
