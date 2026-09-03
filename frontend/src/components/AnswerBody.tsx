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

/** A doc URL is a safe link only when it is http(s) or a site-relative path. */
function isSafeHref(url: string): boolean {
  return /^https?:\/\//i.test(url) || url.startsWith("/");
}

function renderSpans(
  spans: Span[],
  groupFor: (marker: string) => SourceGroup | undefined,
  activeKey: string | null,
  onChipClick: (key: string) => void,
  keyPrefix: string,
) {
  return spans.map((span, i) => {
    const k = `${keyPrefix}-${i}`;
    if (span.kind === "bold") return <strong key={k}>{span.value}</strong>;
    if (span.kind === "code") return <code key={k} className="answer-code">{span.value}</code>;
    if (span.kind === "marker") {
      const group = groupFor(span.value);
      // A marker with no matching card: validation can drop a citation, and
      // markers can be gapped. Show it as the plain text the model wrote rather
      // than a chip that leads nowhere.
      if (!group) return <span key={k}>{`[${span.value}]`}</span>;
      const active = activeKey === group.key;
      const label = `Source ${span.value}: ${group.title}`;
      // The brackets are REAL characters, not CSS content, so copied text keeps
      // the plain ``[n]`` form the persisted answer uses.
      const inner = (
        <>
          <span className="chip-bracket" aria-hidden="true">[</span>
          {span.value}
          <span className="chip-bracket" aria-hidden="true">]</span>
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
          title={group.title}
          aria-label={label}
          data-marker={span.value}
          onClick={() => onChipClick(group.key)}
        >
          {inner}
        </a>
      ) : (
        <span key={k} className={cls} title={group.title} aria-label={label} data-marker={span.value}>
          {inner}
        </span>
      );
    }
    return <span key={k}>{span.value}</span>;
  });
}

interface AnswerBodyProps {
  text: string;
  streaming?: boolean;
  sources: Source[];
  /** Shown under the FIRST cited answer of the session only. */
  showLegend?: boolean;
}

export function AnswerBody({ text, streaming, sources, showLegend = false }: AnswerBodyProps) {
  // Which document is currently highlighted. Clicking a chip sets it; hovering a
  // card sets it — one piece of state drives both directions of the tie.
  const [activeKey, setActiveKey] = useState<string | null>(null);

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
                  {renderSpans(item, groupFor, activeKey, setActiveKey, `${bi}-${ii}`)}
                </li>
              ))}
            </ul>
          ) : (
            <span key={bi} className="answer-para">
              {renderSpans(block.spans, groupFor, activeKey, setActiveKey, `${bi}`)}
            </span>
          ),
        )}
        {streaming && <span className="typing-cursor" />}
      </div>

      {!streaming && groups.length > 0 && (
        <div className="sources">
          {groups.map((g) => (
            <div
              key={g.key}
              className={"source-card" + (activeKey === g.key ? " is-active" : "")}
              onMouseEnter={() => setActiveKey(g.key)}
              onMouseLeave={() => setActiveKey(null)}
              data-markers={g.markers.join(",")}
            >
              <span className="source-number">{g.markers.join(", ")}</span>
              <div className="source-info">
                <div className="source-title">{g.title}</div>
                <div className="source-url">{g.url}</div>
              </div>
            </div>
          ))}
          {showLegend && (
            <p className="citation-legend">
              Numbers link each sentence to its source below.
            </p>
          )}
        </div>
      )}
    </>
  );
}
