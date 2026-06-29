/**
 * Top navigation bar — brutalist-lite spec.
 *
 *   - Fixed, full-width, h-20.
 *   - White/90 backdrop-blur over the page content.
 *   - Brand mark on the left: "CITEVYN" in Anton 3xl with a
 *     golden yellow period on the end.
 *   - Centre: nav links in Satoshi medium.
 *   - Right: text-only Login + pill button (charcoal bg, white text,
 *     px-6, rounded-full) + a style switcher that toggles between
 *     the brutalist ("core") and softly landing experiences.
 *
 * No router — switching views is a callback to the parent
 * (see ``App.tsx``) so this component stays a dumb presentation
 * piece.
 */

import type { Theme, StyleId } from "../App";

export type ViewId = "chat" | "exact" | "about";

interface ViewDef {
  id: ViewId;
  label: string;
}

const VIEWS: ReadonlyArray<ViewDef> = [
  { id: "chat", label: "Chat" },
  { id: "exact", label: "Exact search" },
  { id: "about", label: "About" },
];

interface StyleDef {
  id: StyleId;
  label: string;
}

const STYLES: ReadonlyArray<StyleDef> = [
  { id: "core", label: "Core" },
  { id: "universal", label: "Universal" },
  { id: "softly", label: "Softly" },
  { id: "devtools", label: "DevTools" },
  { id: "devtools-alt", label: "Terminal" },
];

interface TopBarProps {
  view: ViewId;
  onViewChange: (view: ViewId) => void;
  theme: Theme;
  onThemeToggle: () => void;
  styleId: StyleId;
  onStyleChange: (styleId: StyleId) => void;
}

export function TopBar({
  view,
  onViewChange,
  theme,
  onThemeToggle,
  styleId,
  onStyleChange,
}: TopBarProps) {
  return (
    <header className="topbar" role="banner">
      <a
        className="topbar__brand"
        href="#"
        onClick={(e) => {
          e.preventDefault();
          onViewChange("chat");
        }}
        aria-label="CiteVyn — go to home"
        data-testid="brand-link"
      >
        <span className="topbar__brandmark">
          CITEVYN<span className="topbar__brandmark-period">.</span>
        </span>
      </a>

      <nav className="topbar__nav" aria-label="Primary">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            type="button"
            className={"topbar__nav-link" + (view === v.id ? " topbar__nav-link--active" : "")}
            aria-current={view === v.id ? "page" : undefined}
            onClick={() => onViewChange(v.id)}
            data-testid={`nav-${v.id}`}
          >
            {v.label}
          </button>
        ))}
      </nav>

      <div className="topbar__actions">
        <div
          className="topbar__style-switch"
          role="group"
          aria-label="Landing style"
        >
          {STYLES.map((s) => (
            <button
              key={s.id}
              type="button"
              className={
                "topbar__style-btn" +
                (styleId === s.id ? " topbar__style-btn--active" : "")
              }
              aria-pressed={styleId === s.id}
              onClick={() => onStyleChange(s.id)}
              data-testid={`style-${s.id}`}
            >
              {s.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="topbar__login"
          onClick={onViewChange.bind(null, "about")}
          data-testid="btn-login"
        >
          Login
        </button>
        <button
          type="button"
          className="topbar__cta"
          onClick={() => onViewChange("chat")}
          data-testid="btn-try-citevyn"
        >
          Try CiteVyn
        </button>
        <button
          type="button"
          className="topbar__theme"
          aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          onClick={onThemeToggle}
          data-testid="theme-toggle"
        >
          {theme === "dark" ? <SunIcon /> : <MoonIcon />}
        </button>
      </div>
    </header>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  );
}