/**
 * The tiny markdown subset chat answers are allowed to use (#303).
 *
 * WHY A HAND-ROLLED PARSER
 * ------------------------
 * A markdown library costs 8-12 kB gzip and the eager bundle has a few hundred
 * bytes of headroom. The model is constrained to a deliberately small subset in
 * the system prompt (`backend/app/llm/prompts.py`), and this parses exactly that
 * subset — bold, inline code, "-" bullets, and `[n]` citation markers.
 *
 * WHY IT RETURNS DATA, NOT HTML
 * -----------------------------
 * `dangerouslySetInnerHTML` is used ZERO times in this codebase: every string
 * reaches the DOM through React's auto-escaping text path. A hand-rolled
 * markdown renderer that emitted an HTML string would be the first place that
 * needed an escaper, and an injection surface by construction. So this returns
 * plain data, the caller maps it to elements, and every `value` below lands in
 * a text position that React escapes. Anything this parser does not recognise
 * stays literal text — it is never markup.
 *
 * The parser also runs on EVERY streamed chunk (the chat dispatches cumulative
 * text), so it must tolerate half-written input: `**bold` and `[1` with no
 * closing delimiter yet are literal text, not a swallowed remainder.
 */

/** An inline run within a line. `value` is always plain text for React to escape. */
export type Span =
  | { kind: "text"; value: string }
  | { kind: "code"; value: string }
  /** A citation marker. `value` holds the number, normalised, e.g. `"3"`. */
  | { kind: "marker"; value: string }
  /** Bold holds SPANS, not a string: a citation inside bold is still a citation.
   *  `**Rate limits: 30/hour [1]**` is ordinary output now that the prompt asks
   *  for bold, and flattening it to text left the marker inert — the exact defect
   *  this feature exists to fix. */
  | { kind: "bold"; spans: Span[] };

export type Block =
  | { kind: "para"; spans: Span[] }
  | { kind: "list"; items: Span[][] };

/** `- ` or `* ` at the start of a line — the only block-level construct in the
 *  subset. Both markers are accepted even though the prompt asks for `-`: the
 *  first live answer after this shipped used `*`, and every bullet rendered a
 *  literal asterisk. The prompt CONSTRAINS the model, it does not compel it, so
 *  the renderer takes the near-universal alternative too. (#303 said so from the
 *  start — "the live model emits ... `*` bullets".)
 *
 *  The trailing space is what keeps this from eating emphasis, and it is doing
 *  ALL of that work: `*notabullet` and `*emphasis* here` stay literal, and
 *  `**bold**` cannot match either, because the character after the first `*` is
 *  another `*` rather than the required whitespace. An explicit bold-first guard
 *  here would be unreachable, so there isn't one. */
const BULLET = /^[ \t]*[-*][ \t]+/;
/** `[12]` — digits only, so prose like "[see below]" is never a marker.
 *  Unbounded `\d+` matches the backend's own marker regex
 *  (``app/llm/validation.py``); a number with no matching citation simply takes
 *  the miss path and renders as plain text. */
const MARKER = /^\[(\d+)\]/;

/** Append text to the trailing text span, or start one. Keeps runs contiguous
 *  so `a**b**c` yields text/bold/text rather than a span per character. */
function pushText(spans: Span[], value: string): void {
  const last = spans[spans.length - 1];
  if (last && last.kind === "text") last.value += value;
  else spans.push({ kind: "text", value });
}

/**
 * Tokenize one line's inline markup.
 *
 * An unterminated delimiter is emitted as literal text and scanning continues
 * after it, so a partially-streamed `**bold` shows the asterisks (briefly)
 * rather than hiding the rest of the answer until the closer arrives.
 */
export function parseInline(line: string): Span[] {
  const spans: Span[] = [];
  let i = 0;
  while (i < line.length) {
    const ch = line[i];

    if (ch === "*" && line[i + 1] === "*") {
      const close = line.indexOf("**", i + 2);
      // Require non-empty content: `****` is literal, not an empty bold.
      if (close > i + 2) {
        // Recurse so code and citation markers inside bold still work. This
        // cannot nest: ``close`` is the FIRST "**" after the opener, so the slice
        // being re-parsed provably contains none, and the recursion is one level
        // deep by construction rather than by a flag.
        spans.push({ kind: "bold", spans: parseInline(line.slice(i + 2, close)) });
        i = close + 2;
        continue;
      }
      pushText(spans, "**");
      i += 2;
      continue;
    }

    if (ch === "`") {
      const close = line.indexOf("`", i + 1);
      if (close > i + 1) {
        spans.push({ kind: "code", value: line.slice(i + 1, close) });
        i = close + 1;
        continue;
      }
      pushText(spans, "`");
      i += 1;
      continue;
    }

    if (ch === "[") {
      const m = MARKER.exec(line.slice(i));
      if (m) {
        // Normalise so `[01]` and `[1]` are the same citation, as the backend's
        // ``int()`` conversion already treats them.
        spans.push({ kind: "marker", value: String(Number(m[1])) });
        i += m[0].length;
        continue;
      }
      pushText(spans, "[");
      i += 1;
      continue;
    }

    pushText(spans, ch);
    i += 1;
  }
  return spans;
}

/**
 * Split an answer into paragraph and list blocks.
 *
 * Consecutive `- ` lines collapse into one list. Everything else accumulates
 * into a paragraph whose newlines are preserved verbatim, so the existing
 * multi-line rendering guard (`tests/answer-format.spec.ts`) still sees one
 * line box per source line.
 */
export function parseAnswer(text: string): Block[] {
  const blocks: Block[] = [];
  let para: string[] = [];
  let items: string[] = [];

  const flushPara = () => {
    if (para.length === 0) return;
    blocks.push({ kind: "para", spans: parseInline(para.join("\n")) });
    para = [];
  };
  const flushList = () => {
    if (items.length === 0) return;
    blocks.push({ kind: "list", items: items.map(parseInline) });
    items = [];
  };

  for (const line of text.split("\n")) {
    const bullet = BULLET.exec(line);
    if (bullet) {
      flushPara();
      items.push(line.slice(bullet[0].length));
    } else {
      flushList();
      para.push(line);
    }
  }
  flushPara();
  flushList();
  return blocks;
}
