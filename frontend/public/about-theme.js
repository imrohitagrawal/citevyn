/**
 * Mirror the SPA's stored theme onto the server-rendered /about page (#84 item 6).
 *
 * The app writes "light" or "dark" to localStorage under "citevyn:theme" and
 * stamps it on <html data-theme> (frontend/src/App.tsx). /about is rendered by
 * the API, not by the bundle, so it never runs that code — without this, a
 * reader who has manually chosen dark and then clicks a citation gets a white
 * page. Everyone who has NOT chosen manually is already handled by the
 * prefers-color-scheme block in about.css, with no JavaScript at all.
 *
 * Loaded as an EXTERNAL same-origin script because the app-wide CSP has no
 * 'unsafe-inline' on script-src (measured in real Chromium: an inline <script>
 * on this origin does not execute). Loaded synchronously in <head> so the
 * attribute is set before first paint — deferring it would show a flash of the
 * wrong theme.
 *
 * Deliberately does nothing else. It sets one attribute and never writes, so it
 * cannot disagree with the app about what the stored preference is.
 */
(function () {
  "use strict";
  try {
    var stored = window.localStorage.getItem("citevyn:theme");
    if (stored === "light" || stored === "dark") {
      document.documentElement.setAttribute("data-theme", stored);
    }
  } catch (err) {
    // localStorage throws outright in some privacy modes. The CSS media query
    // is the fallback, so swallowing this leaves the page correct rather than
    // half-styled by a thrown error in <head>.
    void err;
  }
})();
