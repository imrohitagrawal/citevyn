import { describe, expect, it } from "vitest";
import { parseAnswer, parseInline, type Block } from "./answerFormat";

import type { Span } from "./answerFormat";

const text = (v: string): Span => ({ kind: "text", value: v });
const bold = (v: string): Span => ({ kind: "bold", value: v });
const code = (v: string): Span => ({ kind: "code", value: v });
const marker = (v: string): Span => ({ kind: "marker", value: v });

describe("parseInline — the allowed subset", () => {
  it("reads bold, inline code and citation markers", () => {
    expect(parseInline("Use **npm** to run `codex` [2]")).toEqual([
      text("Use "),
      bold("npm"),
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
    expect(spans.map((s) => s.value).join("")).toBe(input);
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
    expect(parseInline("Use **npm**")).toEqual([text("Use "), bold("npm")]);
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
