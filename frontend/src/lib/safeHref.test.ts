import { describe, expect, it } from "vitest";
import { isSafeHref } from "./safeHref";

describe("isSafeHref — links that must keep working", () => {
  it.each([
    ["site-relative path", "/about"],
    ["site-relative with query", "/about?x=1"],
    ["external https doc", "https://docs.claude.com/en/docs/claude-code"],
    ["external http doc", "http://example.com/x"],
    ["uppercase scheme", "HTTPS://docs.claude.com/x"],
  ])("allows %s", (_l, url) => {
    expect(isSafeHref(url)).toBe(true);
  });
});

describe("isSafeHref — off-origin URLs disguised as site-relative paths", () => {
  // These are the whole reason this is a parser and not a prefix check. The
  // WHATWG parser strips ASCII tab/LF/CR BEFORE parsing, so the last three are
  // `//evil.com` — and a `!url.startsWith("//")` patch returns true for them.
  it.each([
    ["protocol-relative", "//evil.com/phish"],
    ["triple slash", "///evil.com/phish"],
    ["backslash", "/\\evil.com"],
    ["tab-separated", "/\t/evil.com"],
    ["newline-separated", "/\n/evil.com"],
    ["carriage-return-separated", "/\r/evil.com"],
    ["tab plus backslash", "/\t\\evil.com"],
  ])("refuses %s", (_l, url) => {
    expect(isSafeHref(url)).toBe(false);
  });
});

describe("isSafeHref — schemes that must never become a link", () => {
  it.each([
    ["javascript", "javascript:alert(1)"],
    ["mixed case javascript", "JaVaScRiPt:alert(1)"],
    ["tab inside the scheme", "java\tscript:alert(1)"],
    ["newline inside the scheme", "java\nscript:alert(1)"],
    ["leading whitespace", "   javascript:alert(1)"],
    ["data html", "data:text/html,<script>alert(1)</script>"],
    ["vbscript", "vbscript:msgbox(1)"],
    ["file", "file:///etc/passwd"],
    // Built from the CURRENT origin so it cannot pass for the wrong reason: a
    // hardcoded host would differ from the test base and be caught by the ORIGIN
    // check instead. Navigating to a blob can execute script in its origin.
    // (Which guard refuses it is documented in the source: the shape guard gets
    // there first, and the scheme check is a deliberate unreachable backstop.)
    ["blob on this exact origin", `blob:${globalThis.location?.origin ?? "https://localhost"}/0000-1111`],
    ["empty", ""],
  ])("refuses %s", (_l, url) => {
    expect(isSafeHref(url)).toBe(false);
  });
});

describe("isSafeHref — schemeless URLs that look like a host", () => {
  // `docs.claude.com/x` has no scheme, so it resolves against THIS origin and
  // navigates to a same-origin 404 while reading as an external doc link. Every
  // url in `knowledgeBase.ts` is this shape.
  it.each([
    ["bare host + path", "docs.claude.com/en/docs/claude-code/overview"],
    ["bare host", "example.com"],
    ["fragment only", "#section"],
    ["query only", "?q=1"],
    ["relative word", "about"],
  ])("refuses %s", (_l, url) => {
    expect(isSafeHref(url)).toBe(false);
  });
});

describe("isSafeHref — surrounding whitespace", () => {
  it("accepts a legitimate URL padded with whitespace", () => {
    expect(isSafeHref("  https://docs.claude.com/x  ")).toBe(true);
  });

  it("still refuses a dangerous scheme padded with whitespace", () => {
    expect(isSafeHref("  javascript:alert(1)  ")).toBe(false);
  });
});

describe("isSafeHref — credential spoofing", () => {
  it("refuses a URL whose visible host is not the host it navigates to", () => {
    expect(isSafeHref("https://docs.claude.com@evil.com/")).toBe(false);
  });
});
