import { describe, expect, it } from "vitest";
import { parseAnswer, parseInline, type Block } from "./answerFormat";

import type { Span } from "./answerFormat";

const text = (v: string): Span => ({ kind: "text", value: v });
const bold = (...spans: Span[]): Span => ({ kind: "bold", spans });
const code = (v: string): Span => ({ kind: "code", value: v });
const marker = (v: string): Span => ({ kind: "marker", value: v });

describe("parseInline — the allowed subset", () => {
  it("reads bold, inline code and citation markers", () => {
    expect(parseInline("Use **npm** to run `codex` [2]")).toEqual([
      text("Use "),
      bold(text("npm")),
      text(" to run "),
      code("codex"),
      text(" "),
      marker("2"),
    ]);
  });

  it("keeps adjacent text in one run rather than one span per character", () => {
    expect(parseInline("abc")).toEqual([text("abc")]);
  });
});

describe("parseInline — everything outside the subset stays literal text", () => {
  // The security property this file exists to preserve: the parser returns DATA,
  // and anything it does not recognise is `text`, which React escapes. There is
  // no HTML-string path, so there is no escaper to get wrong.
  const notMarkup: Array<[string, string]> = [
    ["raw html", "<script>alert(1)</script>"],
    ["img onerror", '<img src=x onerror="alert(1)">'],
    ["html entity", "&lt;script&gt;"],
    ["markdown link", "[click](javascript:alert(1))"],
    ["markdown image", "![alt](https://evil/x.png)"],
    ["autolink", "<https://evil.example>"],
    ["heading", "# Heading"],
    ["blockquote", "> quoted"],
    ["italic", "*just one star*"],
    ["underscore emphasis", "_underscored_"],
    ["strikethrough", "~~struck~~"],
    ["html comment", "<!-- hidden -->"],
    ["table row", "| a | b |"],
  ];
  it.each(notMarkup)("leaves %s as plain text", (_label, input) => {
    const spans = parseInline(input);
    // Nothing was interpreted as markup...
    expect(spans.every((s) => s.kind === "text")).toBe(true);
    // ...and the characters survive verbatim, so the reader sees what was written.
    expect(spans.map((s) => (s.kind === "bold" ? "" : s.value)).join("")).toBe(input);
  });

  it("never treats a bracketed word as a citation marker", () => {
    expect(parseInline("[see below] and [1a]")).toEqual([text("[see below] and [1a]")]);
  });
});

describe("parseInline — tolerates half-written input mid-stream", () => {
  // The parser runs on every streamed chunk, so it sees prefixes of the answer.
  it("shows an unterminated bold as literal asterisks, not a swallowed tail", () => {
    expect(parseInline("Use **npm to run")).toEqual([text("Use **npm to run")]);
  });

  it("shows an unterminated code span literally", () => {
    expect(parseInline("Run `codex --model")).toEqual([text("Run `codex --model")]);
  });

  it("shows an unterminated marker literally", () => {
    expect(parseInline("Grounded in [1")).toEqual([text("Grounded in [1")]);
  });

  it("does not render an empty bold from four asterisks", () => {
    expect(parseInline("****")).toEqual([text("****")]);
  });

  it("resolves the moment the closing delimiter streams in", () => {
    expect(parseInline("Use **npm**")).toEqual([text("Use "), bold(text("npm"))]);
  });
});

describe("parseInline — delimiter edge cases", () => {
  it("renders single-character bold and code", () => {
    expect(parseInline("**a**")).toEqual([bold(text("a"))]);
    expect(parseInline("`a`")).toEqual([code("a")]);
  });

  it("does not render an empty code span from two backticks", () => {
    expect(parseInline("``")).toEqual([text("``")]);
  });

  it("keeps whitespace inside delimiters verbatim", () => {
    expect(parseInline("** a **")).toEqual([bold(text(" a "))]);
    expect(parseInline("`  x  `")).toEqual([code("  x  ")]);
  });

  it("only matches a marker at the current position, never mid-string", () => {
    // Without the ^ anchor the regex would find "[1]" later in the string and
    // consume the wrong three characters.
    expect(parseInline("[x] [1]")).toEqual([text("[x] "), marker("1")]);
  });
});

describe("parseAnswer — bullet edge cases", () => {
  it("accepts an indented bullet and a tab-separated one", () => {
    expect(parseAnswer("  - indented")).toEqual([
      { kind: "list", items: [[text("indented")]] },
    ]);
    expect(parseAnswer("-\ttabbed")).toEqual([
      { kind: "list", items: [[text("tabbed")]] },
    ]);
  });
});

describe("parseInline — markup nested inside bold still works", () => {
  // The prompt now ASKS for bold, so `**Rate limits: 30/hour [1]**` is ordinary
  // output. Flattening bold to a string left that marker inert — the exact
  // defect this feature exists to fix, on the most likely sentence shape.
  it("keeps a citation marker inside bold a real marker", () => {
    expect(parseInline("**Rate limits: 30/hour [1]**")).toEqual([
      bold(text("Rate limits: 30/hour "), marker("1")),
    ]);
  });

  it("keeps inline code inside bold", () => {
    expect(parseInline("**run `npm i` first**")).toEqual([
      bold(text("run "), code("npm i"), text(" first")),
    ]);
  });

  it("does not nest bold within bold", () => {
    // `**a **b** c**` closes at the first `**` pair; the remainder is literal.
    const spans = parseInline("**a **b** c**");
    expect(spans[0]).toEqual(bold(text("a ")));
  });
});

describe("parseInline — marker numbering matches the backend", () => {
  it("normalises a zero-padded marker, as the backend's int() does", () => {
    expect(parseInline("[01]")).toEqual([marker("1")]);
    expect(parseInline("[007]")).toEqual([marker("7")]);
  });

  it("accepts markers of any length, like the backend regex", () => {
    // The backend uses an unbounded \d+ and hard-fails out-of-range numbers, so
    // a client cap here would silently disagree about what a marker even is.
    expect(parseInline("[1234]")).toEqual([marker("1234")]);
  });
});

describe("parseAnswer — blocks", () => {
  it("keeps newlines inside a paragraph so multi-line answers still render as lines", () => {
    expect(parseAnswer("Alpha.\nBeta.\nGamma.")).toEqual([
      { kind: "para", spans: [text("Alpha.\nBeta.\nGamma.")] },
    ]);
  });

  it("collapses consecutive '- ' lines into one list", () => {
    expect(parseAnswer("Options:\n- first [1]\n- second\nAfter.")).toEqual<Block[]>([
      { kind: "para", spans: [text("Options:")] },
      { kind: "list", items: [[text("first "), marker("1")], [text("second")]] },
      { kind: "para", spans: [text("After.")] },
    ]);
  });

  it("does not treat a hyphenated word or a bare dash as a bullet", () => {
    expect(parseAnswer("well-known\n-notabullet")).toEqual([
      { kind: "para", spans: [text("well-known\n-notabullet")] },
    ]);
  });

  it("returns nothing renderable for empty input", () => {
    expect(parseAnswer("")).toEqual([{ kind: "para", spans: [] }]);
  });
});
