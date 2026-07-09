/**
 * App.tsx - Minimal wrapper for the new landing page.
 *
 * Replaces the old multi-style architecture with a single landing page.
 * Manages global theme state and delegates to LandingPage.
 */

import { useState, useEffect } from "react";
import { LandingPage } from "./components/LandingPage";

// Theme management
export type Theme = "light" | "dark";
export const THEME_STORAGE_KEY = "citevyn:theme";

function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setTheme] = useState<Theme>("light");

  useEffect(() => {
    // Read initial theme
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    if (stored === "light" || stored === "dark") {
      setTheme(stored);
      document.documentElement.setAttribute("data-theme", stored);
    } else {
      // Match system preference
      const media = window.matchMedia("(prefers-color-scheme: dark)");
      const preferred = media.matches ? "dark" : "light";
      setTheme(preferred);
      document.documentElement.setAttribute("data-theme", preferred);
    }
  }, []);

  const toggleTheme = (newTheme: Theme) => {
    setTheme(newTheme);
    localStorage.setItem(THEME_STORAGE_KEY, newTheme);
    document.documentElement.setAttribute("data-theme", newTheme);
  };

  return [theme, toggleTheme];
}

function App() {
  const [theme, toggleTheme] = useTheme();

  return (
    <div className={theme}>
      <LandingPage theme={theme} onThemeChange={toggleTheme} />
    </div>
  );
}

export default App;