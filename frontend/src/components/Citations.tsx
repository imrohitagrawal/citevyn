/**
 * Citation markers and citation list.
 *
 * The marker pattern: a clickable ``[1]`` / ``[2]`` chip with a
 * brand-coloured background. Hovering or focusing a marker
 * surfaces a popover with the source name, document title, and
 * the chunk id; clicking the chip (or pressing Enter while
 * focused) opens the source URL in a new tab.
 *
 * The full citation list, also numbered, is rendered at the
 * bottom of the message bubble so the same numbers line up
 * whether the reader scans the inline markers or scrolls down.
 *
 * We intentionally do NOT inject citation markers into the
 * answer text. The stub LLM in the backend does not emit
 * ``[1]`` placeholders, so any markers we render are an
 * invention; the spec leaves the rendering decision to the UI.
 * The cleanest thing is to render them as a parallel list and
 * keep the answer prose untouched.
 */

import { useState } from "react";
import type { Citation } from "../lib/types";

interface CitationsProps {
  citations: Citation[];
}

/**
 * Renders the inline marker row. The popover is opened on
 * hover (mouse) or focus (keyboard) — focus is the path
 * screen-reader users take, and the same popover content is
 * available to both.
 */
export function CitationMarkers({ citations }: CitationsProps) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);

  if (citations.length === 0) return null;

  return (
    <div className="citation-markers" aria-label="Citations">
      <span className="citation-markers__label tiny muted">Sources:</span>
      {citations.map((c, i) => {
        const isOpen = openIndex === i;
        return (
          <span
            key={`${c.chunk_id}-${i}`}
            className="citation-marker-wrap"
            onMouseEnter={() => setOpenIndex(i)}
            onMouseLeave={() => setOpenIndex(null)}
            onFocus={() => setOpenIndex(i)}
            onBlur={() => setOpenIndex(null)}
          >
            <a
              className="citation-marker"
              href={c.url}
              target="_blank"
              rel="noreferrer noopener"
              aria-label={`Citation ${i + 1}: ${c.source_name} — ${c.title}`}
              onClick={() => setOpenIndex(null)}
            >
              {i + 1}
            </a>
            {isOpen && (
              <span className="citation-popover" role="tooltip">
                <span className="citation-popover__title">{c.title}</span>
                <span className="citation-popover__source">{c.source_name}</span>
                <span className="citation-popover__chunk mono tiny muted">
                  chunk: {c.chunk_id.slice(0, 8)}…
                </span>
              </span>
            )}
          </span>
        );
      })}
    </div>
  );
}

/**
 * Full citation list, rendered at the bottom of an assistant
 * message. Each row is a clickable card with the same numbered
 * marker so it ties back to the inline references.
 */
export function CitationList({ citations }: CitationsProps) {
  if (citations.length === 0) return null;

  return (
    <section className="citation-list" aria-label="Cited sources">
      <header className="citation-list__header">
        <span className="tiny muted">Cited sources</span>
        <span className="tiny muted">{citations.length}</span>
      </header>
      <ol className="citation-list__items">
        {citations.map((c, i) => (
          <li key={`${c.chunk_id}-${i}`} className="citation-row">
            <a
              className="citation-row__link"
              href={c.url}
              target="_blank"
              rel="noreferrer noopener"
            >
              <span className="citation-marker citation-marker--static" aria-hidden="true">
                {i + 1}
              </span>
              <span className="citation-row__body">
                <span className="citation-row__title">{c.title}</span>
                <span className="citation-row__meta">
                  {c.source_name} · chunk {c.chunk_id.slice(0, 8)}…
                </span>
              </span>
              <span className="citation-row__open" aria-hidden="true">↗</span>
            </a>
          </li>
        ))}
      </ol>
    </section>
  );
}