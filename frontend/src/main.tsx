/**
 * Application entry point.
 *
 * Mounts :func:`App` into ``<div id="root">`` (see ``index.html``).
 * Imports the global stylesheets in the order required for the
 * cascade: tokens first, then reset, then app-level styles.
 * Component-level styles are imported alongside their components
 * (Vite extracts them per-route by default).
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./styles/tokens.css";
import "./styles/reset.css";
import "./styles/app.css";
import "./styles/chat-enhancements.css";
import "./styles/softly.css";
import "./styles/devtools.css";
import "./styles/universal.css";
import { App } from "./App";

const root = document.getElementById("root");
if (!root) {
  throw new Error("#root element missing from index.html");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
