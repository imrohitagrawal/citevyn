/**
 * LiveCitationTrace — the working citation trace panel.
 *
 * Three chunk cards on the left, a sample answer on the right. Clicking a
 * card highlights it; clicking a `[1]` / `[2]` / `[3]` chip in the answer
 * highlights the matching card. The point of this component is to *show*
 * the actual product interaction (chunk selection + citation chip wiring)
 * rather than a static screenshot of it.
 *
 * Used by:
 *   - DevToolsApp (DevTools-style chrome around it)
 *   - UniversalApp (Core-style chrome around it)
 *
 * The component is purely visual — it does not talk to the API. The
 * caller decides what chunks to show.
 */

import { useMemo } from "react";

export interface CitationChunk {
  id: string;
  /** Plain-English label shown on the chunk card. */
  title: string;
  /** Plain-English source citation shown below the label. */
  source: string;
  /** Optional byte-span / page reference, shown when the card is active. */
  span?: string;
  /** Relevance score, 0–1. */
  score: number;
  /** Body text shown when the card is active. */
  text: string;
}

interface LiveCitationTraceProps {
  chunks: ReadonlyArray<CitationChunk>;
  activeChunk: string;
  onSelectChunk: (id: string) => void;
  /** Optional header label (e.g. "citation trace" or "How it answers"). */
  title?: string;
  /** Optional right-aligned meta in the header (e.g. "3 chunks · 142ms"). */
  meta?: string;
}

export function LiveCitationTrace({
  chunks,
  activeChunk,
  onSelectChunk,
  title = "citation trace",
  meta,
}: LiveCitationTraceProps) {
  const active = useMemo(
    () => chunks.find((c) => c.id === activeChunk) ?? chunks[0],
    [chunks, activeChunk],
  );

  return (
    <div className="ct" aria-label="Citation trace demo">
      <div className="ct__chrome">
        <div className="ct__chrome-dots" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <span className="ct__chrome-title">{title}</span>
        {meta && <span className="ct__chrome-meta">{meta}</span>}
      </div>

      <div className="ct__stage">
        <div className="ct__chunks" role="tablist" aria-label="Retrieved chunks">
          {chunks.map((c, i) => {
            const isActive = c.id === activeChunk;
            return (
              <button
                key={c.id}
                type="button"
                role="tab"
                aria-selected={isActive}
                className={"ct__card" + (isActive ? " ct__card--active" : "")}
                onClick={() => onSelectChunk(c.id)}
              >
                <span className="ct__card-head">
                  <span className="ct__card-tag">[{i + 1}]</span>
                  <span className="ct__card-title">{c.title}</span>
                  <span className="ct__card-score">
                    relevance {(c.score * 100).toFixed(0)}%
                  </span>
                </span>
                <span className="ct__card-source">{c.source}</span>
                {isActive && (
                  <span className="ct__card-body">{c.text}</span>
                )}
                {isActive && c.span && (
                  <span className="ct__card-span">{c.span}</span>
                )}
              </button>
            );
          })}
        </div>

        <div className="ct__answer" aria-label="Sample answer with citations">
          <div className="ct__answer-label">answer</div>
          <p className="ct__answer-text">
            CiteVyn searches your docs, picks the passages that answer your question
            {" "}
            <button
              type="button"
              className="ct__cite"
              aria-label={`Jump to source 1 (${chunks[0]?.title ?? ""})`}
              onClick={() => chunks[0] && onSelectChunk(chunks[0].id)}
            >
              [1]
            </button>
            . Each passage gets a relevance score, and CiteVyn attaches a numbered
            citation to every claim it makes
            {" "}
            <button
              type="button"
              className="ct__cite"
              aria-label={`Jump to source 2 (${chunks[1]?.title ?? ""})`}
              onClick={() => chunks[1] && onSelectChunk(chunks[1].id)}
            >
              [2]
            </button>
            . If a question isn't covered in the indexed source, CiteVyn says so
            plainly instead of guessing
            {" "}
            <button
              type="button"
              className="ct__cite"
              aria-label={`Jump to source 3 (${chunks[2]?.title ?? ""})`}
              onClick={() => chunks[2] && onSelectChunk(chunks[2].id)}
            >
              [3]
            </button>
            .
          </p>
          <p className="ct__answer-foot" aria-hidden="true">
            Active source: <strong>{active?.title}</strong> · relevance{" "}
            {active ? Math.round(active.score * 100) : 0}%
          </p>
        </div>
      </div>
    </div>
  );
}