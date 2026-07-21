/**
 * Adapter between the API citation shape and the demo ``Source`` shape.
 *
 * The landing UI was built against a canned ``Source`` (``{n,title,url}``)
 * long before the backend existed. The live API returns richer
 * ``Citation`` objects (``{source_name,title,url,chunk_id}``). Rather than
 * thread the API shape through every view, we normalize at the boundary:
 * the chat renders ``Source`` regardless of whether the answer came from
 * the KB or the backend.
 *
 * ``n`` is a 1-based display index — the UI shows it as a superscript
 * citation marker, so it must be a stable, human-readable string, not the
 * backend ``chunk_id`` (a UUID that means nothing to the reader).
 */
import type { Citation } from "./types";
import type { Source } from "../data/knowledgeBase";

/**
 * Convert an ordered list of API citations into the demo ``Source[]`` the
 * chat renders. Numbering is 1-based and follows array order (the backend
 * already returns citations in relevance/appearance order).
 *
 * Field fallbacks keep the UI robust to sparse backend data:
 *   - ``title`` → ``source_name`` → ``"Source {n}"`` (never blank).
 *   - ``url`` → ``""`` (the source chip renders as non-clickable text).
 */
export function citationsToSources(citations: Citation[]): Source[] {
  return citations.map((citation, index) => {
    // Number by the marker the model actually wrote, not by array position
    // (#215). An answer citing [1] and [3] returns TWO citations; by position
    // they would render as cards 1 and 2 while the prose says [3], pointing the
    // reader at a card that does not exist.
    //
    // The runtime check is not belt-and-braces: `api.ts` returns `parsed as T`,
    // a bare cast with no wire validation, so the required type is a
    // compile-time promise the network does not keep. A response without a
    // usable marker falls back to position rather than rendering `undefined`.
    const n = String(Number.isInteger(citation.marker) ? citation.marker : index + 1);
    return {
      n,
      title: citation.title || citation.source_name || `Source ${n}`,
      url: citation.url || "",
    };
  });
}
