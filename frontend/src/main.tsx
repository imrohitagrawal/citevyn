/**
 * Application entry point.
 *
 * Mounts App into <div id="root"> (see index.html).
 * Imports styles: tokens, reset, and landing styles.
 * Component styles are imported alongside LandingPage (Vite extracts them).
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

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