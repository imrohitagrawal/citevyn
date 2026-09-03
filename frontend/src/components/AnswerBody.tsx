/**
 * Renders a bot answer: the allowed markdown subset, citation chips, and the
 * source cards they point at (#303).
 *
 * SAFETY: this never builds an HTML string. ``parseAnswer`` returns data and
 * every string below lands in a text position, so React escapes it — the same
 * property the rest of the app has (``dangerouslySetInnerHTML`` appears zero
 * times in this codebase). Anything outside the subset is literal text by
 * construction, not by escaping.
 */

import { useState } from "react";
import { parseAnswer, type Span } from "../lib/answerFormat";
import type { Source } from "../data/knowledgeBase";
import { isSafeHref } from "../lib/safeHref";

/** A source card, after collapsing every citation that points at one document. */
export interface SourceGroup {
  /** Stable key: the URL when there is one, else the title. */
  key: string;
  title: string;
  url: string;
  /** Every marker this document backs, in numeric order. */
  markers: string[];
}

/**
 * Collapse citations by document.
 *
 * One page cited five times produced five identical cards. Grouping by URL (or
 * by title when a citation carries no URL) leaves one card whose badge lists
 * every marker it backs. Order follows first appearance, so the cards still read
 * top-to-bottom in the order the answer cites them.
 */
export function groupSources(sources: Source[]): SourceGroup[] {
  const byKey = new Map<string, SourceGroup>();
  for (const src of sources) {
    const key = src.url || src.title;
    const existing = byKey.get(key);
    if (existing) {
      if (!existing.markers.includes(src.n)) existing.markers.push(src.n);
    } else {
      byKey.set(key, { key, title: src.title, url: src.url, markers: [src.n] });
    }
  }
  const groups = [...byKey.values()];
  for (const g of groups) g.markers.sort((a, b) => Number(a) - Number(b));
  return groups;
}

function renderSpans(
  spans: Span[],
  groupFor: (marker: string) => SourceGroup | undefined,
  activeKey: string | null,
  onChipFocus: (key: string | null) => void,
  keyPrefix: string,
) {
  return spans.map((span, i) => {
    const k = `${keyPrefix}-${i}`;
    if (span.kind === "bold") {
      // Recurse: markers and code inside bold keep working.
      return (
        <strong key={k}>
          {renderSpans(span.spans, groupFor, activeKey, onChipFocus, `${k}b`)}
        </strong>
      );
    }
    if (span.kind === "code") return <code key={k} className="answer-code">{span.value}</code>;
    if (span.kind === "marker") {
      const group = groupFor(span.value);
      // A marker with no matching card: validation can drop a citation, and
      // markers can be gapped. Show it as the plain text the model wrote rather
      // than a chip that leads nowhere.
      if (!group) return <span key={k}>{`[${span.value}]`}</span>;
      const active = activeKey === group.key;
      // The brackets are REAL characters, not CSS content, so copied text keeps
      // the plain ``[n]`` form the persisted answer uses. They are NOT
      // ``aria-hidden``: on the linked chip the ``aria-label`` wins anyway, and on
      // the inert one they are the only thing that makes it read as "[2]" rather
      // than a bare "2".
      const inner = (
        <>
          <span className="chip-bracket">[</span>
          {span.value}
          <span className="chip-bracket">]</span>
        </>
      );
      const cls = "citation-chip" + (active ? " is-active" : "");
      return isSafeHref(group.url) ? (
        <a
          key={k}
          className={cls}
          href={group.url}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Source ${span.value}: ${group.title}`}
          data-marker={span.value}
          // Keyboard parity for the chip -> card tie. Without `onFocus`, tabbing
          // to a chip highlights nothing and the affordance is mouse-only.
          // `onClick` is kept because WebKit does not always focus an anchor on
          // click.
          onClick={() => onChipFocus(group.key)}
          onFocus={() => onChipFocus(group.key)}
          onBlur={() => onChipFocus(null)}
        >
          {inner}
        </a>
      ) : (
        // No ``aria-label`` here: ARIA 1.2 prohibits it on role=generic, and a
        // conforming reader that drops it would announce a bare "2".
        <span key={k} className={cls} title={group.title} data-marker={span.value}>
          {inner}
        </span>
      );
    }
    return <span key={k}>{span.value}</span>;
  });
}

/**
 * Does this answer actually render at least one citation chip?
 *
 * `ChatView` uses this to decide where the once-per-session legend goes. It must
 * be the SAME logic the renderer uses, not a regex approximation: a `[9]` with
 * no matching source, or a marker the parser does not recognise, produces no
 * chip, and a legend explaining chips that are not there is worse than none.
 */
export function hasCitationChips(text: string, sources: Source[]): boolean {
  const markers = new Set<string>();
  for (const g of groupSources(sources)) for (const m of g.markers) markers.add(m);
  if (markers.size === 0) return false;
  const walk = (spans: Span[]): boolean =>
    spans.some((span) =>
      span.kind === "marker"
        ? markers.has(span.value)
        : span.kind === "bold"
          ? walk(span.spans)
          : false,
    );
  return parseAnswer(text).some((block) =>
    block.kind === "list" ? block.items.some(walk) : walk(block.spans),
  );
}

interface AnswerBodyProps {
  text: string;
  streaming?: boolean;
  sources: Source[];
  /** Shown under the FIRST cited answer of the session only. */
  showLegend?: boolean;
}

export function AnswerBody({ text, streaming, sources, showLegend = false }: AnswerBodyProps) {
  // Which document is highlighted, tracked per INPUT rather than as one shared
  // value. With a single `activeKey` the two inputs clobber each other in both
  // directions (reproduced in Chromium, Firefox and WebKit): moving the mouse off
  // a card wiped the focus ring of a chip that was still focused, and tabbing
  // away cleared a card the mouse was still hovering. Keyboard wins when both
  // are live, because focus is the more deliberate signal.
  const [focusedKey, setFocusedKey] = useState<string | null>(null);
  const [hoveredKey, setHoveredKey] = useState<string | null>(null);
  const activeKey = focusedKey ?? hoveredKey;

  const groups = groupSources(sources);
  const byMarker = new Map<string, SourceGroup>();
  for (const g of groups) for (const m of g.markers) byMarker.set(m, g);
  const groupFor = (marker: string) => byMarker.get(marker);

  const blocks = parseAnswer(text);

  return (
    <>
      <div className={"message-body" + (streaming ? " streaming" : "")}>
        {blocks.map((block, bi) =>
          block.kind === "list" ? (
            <ul key={bi} className="answer-list">
              {block.items.map((item, ii) => (
                <li key={ii}>
                  {renderSpans(item, groupFor, activeKey, setFocusedKey, `${bi}-${ii}`)}
                </li>
              ))}
            </ul>
          ) : (
            <span key={bi} className="answer-para">
              {renderSpans(block.spans, groupFor, activeKey, setFocusedKey, `${bi}`)}
            </span>
          ),
        )}
        {streaming && <span className="typing-cursor" />}
      </div>

      {!streaming && groups.length > 0 && showLegend && (
        <p className="citation-legend">Numbers link each sentence to its source below.</p>
      )}

      {!streaming && groups.length > 0 && (
        <div className="sources">
          {groups.map((g) => (
            <div
              key={g.key}
              className={"source-card" + (activeKey === g.key ? " is-active" : "")}
              onMouseEnter={() => setHoveredKey(g.key)}
              onMouseLeave={() => setHoveredKey(null)}
            >
              <span className="source-number">{g.markers.join(", ")}</span>
              <div className="source-info">
                <div className="source-title">{g.title}</div>
                <div className="source-url">{g.url}</div>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
