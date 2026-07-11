/**
 * Header — Sticky navigation bar.
 */

interface HeaderProps {
  themeLabel: string;
  themeGlyph: string;
  onThemeToggle: () => void;
  onAskClick: () => void;
  onNavClick: (e: React.MouseEvent, id: string) => void;
}

export function Header({
  themeLabel,
  themeGlyph,
  onThemeToggle,
  onAskClick,
  onNavClick,
}: HeaderProps) {
  return (
    <header className="header">
      <div className="header-container">
        <a href="#top" onClick={(e) => onNavClick(e, "top")} className="logo">
          <span>CiteVyn</span>
          <sup className="logo-badge">01</sup>
        </a>

        <nav>
          {["who", "how", "demo", "pricing", "faq"].map((id, i) => (
            <a
              key={id}
              href={`#${id}`}
              onClick={(e) => onNavClick(e, id)}
              className="nav-link"
            >
              {["Who it's for", "How it works", "Demo", "Pricing", "FAQ"][i]}
            </a>
          ))}
        </nav>

        <div className="controls">
          <button onClick={onThemeToggle} className="theme-toggle">
            <span>{themeGlyph}</span>
            {themeLabel}
          </button>
          <button onClick={onAskClick} className="cta-button">
            Try the demo
          </button>
        </div>
      </div>
    </header>
  );
}