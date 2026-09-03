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
export function isSafeHref(url: string): boolean {
  if (!url) return false;
  const base =
    typeof window !== "undefined" && window.location
      ? window.location.href
      : "https://localhost/";
  try {
    const parsed = new URL(url, base);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
    if (parsed.username || parsed.password) return false;
    // Only a URL written as an explicit http(s) address may leave this origin.
    if (!/^https?:\/\//i.test(url) && parsed.origin !== new URL(base).origin) {
      return false;
    }
    return true;
  } catch {
    return false;
  }
}
