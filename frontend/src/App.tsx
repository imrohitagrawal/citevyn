/**
 * Root application component.
 *
 * Manages:
 * - Theme (light/dark, respects OS preference until toggled)
 * - Style variant (core, softly, devtools, devtools-alt, universal, browser-core, editorial-studio)
 * - Session state for the RAG API
 * - View routing (chat, exact, about)
 *
 * The style variant is persisted to localStorage so returning
 * visitors keep their preference.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import type { AskResponse, SessionId } from "./lib/types";
import { ApiClientError } from "./lib/types";

import { ChatView } from "./components/ChatView";
import { ExactSearchView } from "./components/ExactSearchView";
import { AboutView } from "./components/AboutView";
import { LandingView } from "./components/LandingView";
import { SoftlyApp } from "./components/SoftlyApp";
import { DevToolsApp } from "./components/DevToolsApp";
import { TerminalApp } from "./components/TerminalApp";
import { UniversalApp } from "./components/UniversalApp";
import { BrowserCoreApp } from "./components/BrowserCoreApp";
import { EditorialStudioApp } from "./components/EditorialStudioApp";
import { UniversalLandingApp } from "./components/UniversalLandingApp";
import { useSoftChat } from "./lib/useSoftChat";
import { TopBar, type ViewId } from "./components/TopBar";
import { ToastRegion, type Toast, type ToastKind } from "./components/Toast";

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

export type Theme = "light" | "dark";
export type StyleId = "core" | "softly" | "devtools" | "devtools-alt" | "universal" | "browser-core" | "editorial-studio" | "landing";

const THEME_STORAGE_KEY = "citevyn:theme";
const SESSION_STORAGE_KEY = "citevyn:session-id";
const VIEW_STORAGE_KEY = "citevyn:view";
const STYLE_STORAGE_KEY = "citevyn:style";

/**
 * Read the initial theme. Order:
 *   1. ``localStorage[citevyn:theme]`` if set.
 *   2. The user's ``prefers-color-scheme`` media query.
 *   3. ``"light"`` as the fallback.
 */
function readInitialTheme(): Theme {
  if (typeof window === "undefined") return "light";
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
    return "dark";
  }
  return "light";
}

function readInitialStyle(): StyleId {
  if (typeof window === "undefined") return "core";
  // URL query param takes priority so shared links (?style=universal etc.)
  // are respected even when localStorage has a previously-selected style.
  const params = new URLSearchParams(window.location.search);
  const fromUrl = params.get("style");
  if (
    fromUrl === "core" ||
    fromUrl === "softly" ||
    fromUrl === "devtools" ||
    fromUrl === "devtools-alt" ||
    fromUrl === "universal" ||
    fromUrl === "browser-core" ||
    fromUrl === "editorial-studio" ||
    fromUrl === "landing"
  ) {
    return fromUrl;
  }
  const stored = window.localStorage.getItem(STYLE_STORAGE_KEY);
  if (
    stored === "core" ||
    stored === "softly" ||
    stored === "devtools" ||
    stored === "devtools-alt" ||
    stored === "universal" ||
    stored === "browser-core" ||
    stored === "editorial-studio" ||
    stored === "landing"
  ) {
    return stored;
  }
  return "core";
}

/** Apply the theme to the ``<html>`` element. */
function applyTheme(theme: Theme) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-theme", theme);
}

/** Apply the landing style to the ``<html>`` element. */
function applyStyle(style: StyleId) {
  if (typeof document === "undefined") return;
  document.documentElement.setAttribute("data-style", style);
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export function App() {
  const [theme, setTheme] = useState<Theme>(readInitialTheme);
  const [styleId, setStyleId] = useState<StyleId>(readInitialStyle);
  const [view, setView] = useState<ViewId>(() => {
    if (typeof window === "undefined") return "chat";
    const v = window.localStorage.getItem(VIEW_STORAGE_KEY);
    return v === "chat" || v === "exact" || v === "about" ? v : "chat";
  });

  // Session state — id persists; everything else is in memory
  // and resets on reload.
  const [sessionId, setSessionId] = useState<SessionId | null>(() => {
    if (typeof window === "undefined") return null;
    return window.localStorage.getItem(SESSION_STORAGE_KEY);
  });
  const [sessionStartedAt, setSessionStartedAt] = useState<string | null>(null);
  const [messageCount, setMessageCount] = useState(0);
  const [indexVersion, setIndexVersion] = useState<string | null>(null);
  const [answerPolicyVersion, setAnswerPolicyVersion] = useState<string | null>(null);

  // Softly chat is a parallel, self-contained session so the two
  // landing experiences don't fight over message state.
  const softlyChat = useSoftChat();

  // Toast queue.
  const [toasts, setToasts] = useState<Toast[]>([]);

  // Persist theme on change.
  useEffect(() => {
    applyTheme(theme);
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  // Persist + apply the landing style.
  useEffect(() => {
    applyStyle(styleId);
    window.localStorage.setItem(STYLE_STORAGE_KEY, styleId);
  }, [styleId]);

  // Persist view on change.
  useEffect(() => {
    window.localStorage.setItem(VIEW_STORAGE_KEY, view);
  }, [view]);

  // Persist session id on change.
  useEffect(() => {
    if (sessionId) {
      window.localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
    } else {
      window.localStorage.removeItem(SESSION_STORAGE_KEY);
    }
  }, [sessionId]);

  // ---------------------------------------------------------------------
  // Toast helpers
  // ---------------------------------------------------------------------

  const pushToast = useCallback(
    (kind: ToastKind, title: string, message: string, requestId?: string, durationMs = 5000) => {
      const id = Date.now() + Math.floor(Math.random() * 1000);
      setToasts((prev) => [...prev, { id, kind, title, message, requestId, durationMs }]);
    },
    [],
  );

  const dismissToast = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const handleError = useCallback(
    (err: ApiClientError) => {
      if (err.isRateLimited()) {
        pushToast(
          "warning",
          "Rate limit reached",
          "Try again in a minute. The demo allows 30 requests per hour.",
          err.hasEnvelope() && typeof err.body === "object" ? (err.body as Record<string, unknown>).request_id as string | undefined : undefined,
          8000,
        );
      } else if (err.isServerError()) {
        pushToast(
          "error",
          `Server error ${err.status}`,
          err.message,
          err.hasEnvelope() && typeof err.body === "object" ? (err.body as Record<string, unknown>).request_id as string | undefined : undefined,
        );
      } else if (err.status === 0) {
        pushToast("error", "Network error", err.message, undefined, 6000);
      } else {
        pushToast(
          "error",
          `Request failed (${err.status})`,
          err.message,
          err.hasEnvelope() && typeof err.body === "object" ? (err.body as Record<string, unknown>).request_id as string | undefined : undefined,
        );
      }
    },
    [pushToast],
  );

  // ---------------------------------------------------------------------
  // Session lifecycle
  // ---------------------------------------------------------------------

  const handleSessionCreated = useCallback((id: SessionId) => {
    setSessionId(id);
    setSessionStartedAt(new Date().toISOString());
  }, []);

  const handleResponseMetadata = useCallback((response: AskResponse) => {
    setMessageCount((c) => c + 1);
    if (response.source_version_hash) setIndexVersion(response.source_version_hash);
    if (response.answer_policy_version) setAnswerPolicyVersion(response.answer_policy_version);
  }, []);

  const handleNewSession = useCallback(() => {
    setSessionId(null);
    setSessionStartedAt(null);
    setMessageCount(0);
    // Index/policy versions stay — they belong to the backend, not
    // the session.
    setView("chat");
  }, []);

  const handleLaunchChat = useCallback((initialPrompt?: string) => {
    setView("chat");
    // The ChatView will read its own state; we hand off the initial
    // prompt via a window-level event so we don't need to thread
    // state through a router.
    if (initialPrompt && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("citevyn:set-prompt", { detail: initialPrompt }));
    }
  }, []);

  // ---------------------------------------------------------------------
  // Render
  // ---------------------------------------------------------------------

  const main = useMemo(() => {
    switch (view) {
      case "chat":
        return (
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
            onSwitchView={setView}
          />
        );
      case "exact":
        return <ExactSearchView onError={handleError} />;
      case "about":
        return <AboutView />;
    }
  }, [
    view,
    sessionId,
    sessionStartedAt,
    messageCount,
    indexVersion,
    answerPolicyVersion,
    handleSessionCreated,
    handleError,
    handleResponseMetadata,
    handleNewSession,
  ]);

  // Softly, DevTools, Terminal, Universal, BrowserCore, and EditorialStudio
  // are all complete replacements for the brutalist landing when the chat view is
  // active; for the other views we always fall back to the TopBar +
  // brutalist layout so non-chat screens keep the global navigation.
  const showAlternateShell =
    view === "chat" &&
    (styleId === "softly" ||
      styleId === "devtools" ||
      styleId === "devtools-alt" ||
      styleId === "universal" ||
      styleId === "browser-core" ||
      styleId === "editorial-studio") ||
    styleId === "landing";

  const appClass = [
    "app",
    styleId === "softly" ? "app--softly" : "",
    styleId === "devtools" ? "app--devtools" : "",
    styleId === "devtools-alt" ? "app--devtools-alt" : "",
    styleId === "universal" ? "app--universal" : "",
    styleId === "browser-core" ? "app--browser-core" : "",
    styleId === "editorial-studio" ? "app--editorial-studio" : "",
    styleId === "landing" ? "app--landing" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={appClass}>
      {!showAlternateShell && (
        <TopBar
          view={view}
          onViewChange={setView}
          theme={theme}
          onThemeToggle={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          styleId={styleId}
          onStyleChange={setStyleId}
        />
      )}

      {showAlternateShell ? (
        // Each non-core style is fully self-contained: it owns its
        // own nav (or none), its own hero, its own chat surface, and
        // (for styles that hide the top bar) its own style dock so
        // the user can always return to Core.
        <>
          {styleId === "softly" && (
            <SoftlyApp
              messages={softlyChat.messages}
              sessionId={softlyChat.sessionId}
              isBusy={softlyChat.isBusy}
              onSend={softlyChat.send}
              onRetry={softlyChat.retry}
            />
          )}
          {styleId === "devtools" && (
            <DevToolsApp
              sessionId={sessionId}
              sessionStartedAt={sessionStartedAt}
              messageCount={messageCount}
              indexVersion={indexVersion}
              answerPolicyVersion={answerPolicyVersion}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={setView}
            />
          )}
          {styleId === "devtools-alt" && (
            <TerminalApp
              sessionId={sessionId}
              sessionStartedAt={sessionStartedAt}
              messageCount={messageCount}
              indexVersion={indexVersion}
              answerPolicyVersion={answerPolicyVersion}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={setView}
            />
          )}
          {styleId === "universal" && (
            <UniversalApp
              sessionId={sessionId}
              sessionStartedAt={sessionStartedAt}
              messageCount={messageCount}
              indexVersion={indexVersion}
              answerPolicyVersion={answerPolicyVersion}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={setView}
            />
          )}
          {styleId === "browser-core" && (
            <BrowserCoreApp
              sessionId={sessionId}
              sessionStartedAt={sessionStartedAt}
              messageCount={messageCount}
              indexVersion={indexVersion}
              answerPolicyVersion={answerPolicyVersion}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={setView}
            />
          )}
          {styleId === "editorial-studio" && (
            <EditorialStudioApp
              sessionId={sessionId}
              sessionStartedAt={sessionStartedAt}
              messageCount={messageCount}
              indexVersion={indexVersion}
              answerPolicyVersion={answerPolicyVersion}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={setView}
            />
          )}
          {styleId === "landing" && (
            <UniversalLandingApp
              sessionId={sessionId}
              sessionStartedAt={sessionStartedAt}
              messageCount={messageCount}
              indexVersion={indexVersion}
              answerPolicyVersion={answerPolicyVersion}
              onSessionCreated={handleSessionCreated}
              onError={handleError}
              onResponseMetadata={handleResponseMetadata}
              onNewSession={handleNewSession}
              onSwitchView={setView}
            />
          )}
          <StyleDock
            styleId={styleId}
            onStyleChange={setStyleId}
            theme={theme}
            onThemeToggle={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          />
        </>
      ) : (
        <>
          {view === "chat" ? (
            main
          ) : (
            <main className="app__main app__main--standalone" key={view}>
              {main}
            </main>
          )}

          {/* Landing-style framing only shown around the chat surface. */}
          {view === "chat" && (
            <LandingView
              sessionId={sessionId}
              messageCount={messageCount}
              indexVersion={indexVersion}
              answerPolicyVersion={answerPolicyVersion}
              onLaunchChat={handleLaunchChat}
            />
          )}
        </>
      )}

      <ToastRegion toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// StyleDock
//
// Tiny floating switcher shown only when the user is in a non-core
// shell (Softly, DevTools, Terminal, Universal) so they can hop back
// to Core without losing the global TopBar (which is hidden in
// those modes).
// ---------------------------------------------------------------------------

interface StyleDockProps {
  styleId: StyleId;
  onStyleChange: (styleId: StyleId) => void;
  theme: Theme;
  onThemeToggle: () => void;
}

function StyleDock({ styleId, onStyleChange, theme, onThemeToggle }: StyleDockProps) {
  const options: ReadonlyArray<{ id: StyleId; label: string }> = [
    { id: "core", label: "Core" },
    { id: "universal", label: "Universal" },
    { id: "softly", label: "Softly" },
    { id: "devtools", label: "DevTools" },
    { id: "devtools-alt", label: "Terminal" },
    { id: "browser-core", label: "Browser" },
    { id: "editorial-studio", label: "Editorial" },
    { id: "landing", label: "Landing" },
  ];
  return (
    <div className="style-dock" role="group" aria-label="Switch landing style" data-testid="style-dock">
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          className={"style-dock__btn" + (styleId === o.id ? " style-dock__btn--active" : "")}
          aria-pressed={styleId === o.id}
          onClick={() => onStyleChange(o.id)}
          data-testid={`style-dock-${o.id}`}
        >
          {o.label}
        </button>
      ))}
      <span className="style-dock__sep" aria-hidden="true" />
      <button
        type="button"
        className="style-dock__btn style-dock__btn--icon"
        aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
        title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
        onClick={onThemeToggle}
        data-testid="style-dock-theme-toggle"
      >
        {theme === "dark" ? "☀" : "☾"}
      </button>
    </div>
  );
}
