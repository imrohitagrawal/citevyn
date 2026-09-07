/**
 * Header — Sticky navigation bar.
 */
import { AccountMenu } from "./AccountMenu";
import { NAV_SECTIONS } from "../data/navSections";

interface HeaderProps {
  themeLabel: string;
  themeGlyph: string;
  onThemeToggle: () => void;
  onAskClick: () => void;
  onNavClick: (e: React.MouseEvent, id: string) => void;
  /** ADR-0004 PR 9: does the current tab already have chat messages? */
  hasChatHistory?: boolean;
  /** Fires once, right after a successful sign-in/register. */
  onAuthenticated?: (hadChatHistory: boolean) => void;
  /** ADR-0004 PR 10: the caller picked a past session from the history drawer. */
  onResumeSession?: (sessionId: string) => void;
}

export function Header({
  themeLabel,
  themeGlyph,
  onThemeToggle,
  onAskClick,
  onNavClick,
  hasChatHistory,
  onAuthenticated,
  onResumeSession,
}: HeaderProps) {
  return (
    <header className="header">
      <div className="header-container">
        <a href="#top" onClick={(e) => onNavClick(e, "top")} className="logo">
          <span>CiteVyn</span>
          <sup className="logo-badge">01</sup>
        </a>

        <nav>
          {/*
            Rendered FROM ../data/navSections, which also derives the set of
            ids that live in the lazily-loaded strip (#358). Two hand-kept
            lists here and in LandingPage let a new nav link silently become a
            dead link -- reproduced in review.
          */}
          {NAV_SECTIONS.map((section) => (
            <a
              key={section.id}
              href={`#${section.id}`}
              onClick={(e) => onNavClick(e, section.id)}
              className="nav-link"
            >
              {section.label}
            </a>
          ))}
        </nav>

        <div className="controls">
          <button onClick={onThemeToggle} className="theme-toggle">
            <span>{themeGlyph}</span>
            {themeLabel}
          </button>
          <AccountMenu
            hasChatHistory={hasChatHistory}
            onAuthenticated={onAuthenticated}
            onResumeSession={onResumeSession}
          />
          <button onClick={onAskClick} className="cta-button">
            Try the demo
          </button>
        </div>
      </div>
    </header>
  );
}