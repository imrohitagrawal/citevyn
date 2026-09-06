/**
 * Application entry point.
 *
 * Mounts App into <div id="root"> (see index.html).
 * Imports styles: self-hosted @font-face rules, tokens, reset, and landing
 * styles. Component styles are imported alongside LandingPage (Vite extracts
 * them).
 *
 * `fonts.css` comes FIRST so the two @font-face rules are declared before any
 * rule that names `Geist`/`JetBrains Mono` in a font stack. Ordering is not
 * load-bearing for correctness — @font-face is not cascade-ordered — but it
 * keeps the emitted stylesheet readable, and it puts the file that removed the
 * third-party render-blocking request (#365) at the top of the list where the
 * next reader will find it.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/reset.css";
import "./styles/landing.css";
import App from "./App";

const root = document.getElementById("root");
if (!root) {
  throw new Error("#root element missing from index.html");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);