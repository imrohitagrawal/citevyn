/**
 * The single answer to "may this URL become a link?".
 *
 * There were two byte-identical prefix-matching copies of this
 * (`ChatView`'s doc suggestions and `AnswerBody`'s citation chips), and both
 * were wrong in the same way: `url.startsWith("/")` accepts `//evil.com/x`,
 * which is not a site-relative path at all — it is protocol-relative and
 * resolves off-origin.
 *
 * A prefix check cannot be repaired, which is why this parses instead. The
 * WHATWG URL parser strips ASCII tab, LF and CR *before* parsing, so
 * `"/\t/evil.com"` is `//evil.com` — any fix built from `startsWith` misses it
 * (measured: the obvious `!url.startsWith("//")` patch still returns true for
 * the tab, LF and CR forms).
 *
 * Rules:
 *   - the scheme must resolve to http(s) — `javascript:`, `data:`, `vbscript:`
 *     never link;
 *   - a URL the author did NOT write absolutely must stay on this origin;
 *   - embedded credentials are refused, since `https://citevyn.example@evil.com`
 *     reads as one host and navigates to another.
 *
 * An unsafe URL is not an error: the caller renders inert text instead, so a
 * bad corpus row degrades to something unclickable rather than a trap.
 */
export function isSafeHref(raw: string): boolean {
  const url = raw.trim();
  if (!url) return false;
  const absolute = /^https?:\/\//i.test(url);
  // A URL must be one of exactly two shapes: an explicit http(s) address, or a
  // path rooted at "/". Anything else is schemeless — `docs.claude.com/x` reads
  // as a host but resolves against THIS origin, producing a same-origin 404
  // dressed as a documentation link. (Every url in `knowledgeBase.ts` is that
  // shape, so a parser that silently accepted them would turn each demo citation
  // into a broken link the moment demo answers gained markers.)
  if (!absolute && !url.startsWith("/")) return false;
  const base =
    typeof window !== "undefined" && window.location
      ? window.location.href
      : "https://localhost/";
  try {
    const parsed = new URL(url, base);
    // BACKSTOP, currently unreachable — and said plainly rather than left to look
    // like coverage. The shape guard above already refuses everything schemeless,
    // so every URL reaching here resolves to http(s) (verified by enumeration:
    // blob:/javascript:/data:/file:/vbscript: all fail the shape guard first, and
    // a "/"-rooted URL inherits the page's own scheme). It stays because it is
    // the only thing that would still refuse `javascript:` if the shape guard
    // were ever relaxed, and no test can distinguish it while that guard holds.
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
    if (parsed.username || parsed.password) return false;
    // A path-rooted URL must stay on this origin: "//evil.com" and "/\evil.com"
    // both start with "/" but resolve off-site.
    if (!absolute && parsed.origin !== new URL(base).origin) return false;
    return true;
  } catch {
    return false;
  }
}
