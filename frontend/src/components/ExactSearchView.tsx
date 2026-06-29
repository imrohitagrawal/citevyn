/**
 * Exact search view.
 *
 * "Did the user paste a known flag, command, config key, model
 * name, or env var?" — this is the second mental model the
 * product supports alongside the chat. The input is a single
 * search box; results show each term-type as a coloured badge
 * and a snippet of the underlying chunk.
 *
 * The product area is a *required* field for the backend (the
 * same flag in two products can mean different things), so we
 * show a chip row of the supported product areas above the
 * search box. The default is ``claude_code`` because that's
 * the most common demo path.
 *
 * Empty input disables the search button; on submit we keep the
 * last response visible while the user types a new query.
 */

import { useEffect, useRef, useState, type FormEvent } from "react";

import { exactSearch } from "../lib/api";
import type { Domain, ExactSearchHit, ExactSearchResponse } from "../lib/types";
import { ApiClientError } from "../lib/types";
import { domainLabel, termTypeCode, termTypeLabel, shortId } from "../lib/format";

// ---------------------------------------------------------------------------
// Product area chooser
// ---------------------------------------------------------------------------

const PRODUCT_AREAS: ReadonlyArray<{ id: Domain; label: string; description: string }> = [
  { id: "claude_code", label: "Claude Code", description: "Permissions, slash commands, hooks" },
  { id: "claude_api", label: "Claude API", description: "Rate limits, headers, model params" },
  { id: "codex", label: "Codex", description: "CLI flags, env vars" },
  { id: "gemini_api", label: "Gemini", description: "API parameters" },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ExactSearchViewProps {
  onError: (err: ApiClientError) => void;
}

export function ExactSearchView({ onError }: ExactSearchViewProps) {
  const [productArea, setProductArea] = useState<Domain>("claude_code");
  const [term, setTerm] = useState("");
  const [response, setResponse] = useState<ExactSearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    const trimmed = term.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    try {
      const result = await exactSearch({ term: trimmed, product_area: productArea });
      setResponse(result);
    } catch (err) {
      const apiError =
        err instanceof ApiClientError
          ? err
          : new ApiClientError(
              err instanceof Error ? err.message : String(err),
              0,
              String(err),
            );
      onError(apiError);
      setResponse(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="well well--narrow view" aria-label="Exact search">
      <header className="well__header">
        <h1 className="well__title">Exact search</h1>
        <p className="well__subtitle">
          Paste a CLI flag, environment variable, configuration key, model
          name, or error code. CiteVyn looks it up directly in the indexed
          source — no LLM in the loop.
        </p>
      </header>

      <div className="row row--wrap" role="tablist" aria-label="Product area">
        {PRODUCT_AREAS.map((p) => (
          <button
            key={p.id}
            type="button"
            role="tab"
            aria-selected={productArea === p.id}
            className={"chip" + (productArea === p.id ? " chip--active" : "")}
            onClick={() => setProductArea(p.id)}
            data-testid={`chip-${p.id}`}
          >
            <span>{p.label}</span>
            <span className="tiny muted">· {p.description}</span>
          </button>
        ))}
      </div>

      <form className="exact-search__form" onSubmit={onSubmit}>
        <input
          ref={inputRef}
          className="input input--mono"
          type="text"
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          placeholder="--max-tokens, CLAUDE_API_RATE_LIMIT, claude-3-5-sonnet…"
          aria-label="Exact term to look up"
          autoComplete="off"
          spellCheck={false}
          disabled={busy}
          data-testid="exact-search-input"
        />
        <button
          type="submit"
          className="button button--primary"
          disabled={busy || term.trim().length === 0}
          data-testid="exact-search-submit"
        >
          {busy ? "Searching…" : "Search"}
        </button>
      </form>

      {response ? (
        <ExactSearchResults response={response} />
      ) : (
        <div className="card card--inset" style={{ textAlign: "center" }}>
          <span className="muted small">Type a term and press Search.</span>
        </div>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Results
// ---------------------------------------------------------------------------

function ExactSearchResults({ response }: { response: ExactSearchResponse }) {
  if (response.total === 0) {
    return (
      <div className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="card__title">No matches for “{response.query}”</span>
          <span className="badge badge--muted">{response.product_area}</span>
        </div>
        <p className="muted small">
          We didn't find that exact term in the index. Try a related flag
          or a different product area, or ask the chat to explain what the
          term means.
        </p>
      </div>
    );
  }

  return (
    <div className="exact-results">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <span className="small secondary">
          <strong>{response.total}</strong> match{response.total === 1 ? "" : "es"} for “{response.query}”
        </span>
        <span className="tiny muted">
          index <span className="mono">{response.index_version}</span>
        </span>
      </div>
      <ul className="exact-results__list">
        {response.hits.map((hit) => (
          <li key={hit.term_id}>
            <ExactHitCard hit={hit} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function ExactHitCard({ hit }: { hit: ExactSearchHit }) {
  return (
    <article className="exact-hit">
      <header className="exact-hit__header">
        <span className="exact-hit__term mono">{hit.term_text}</span>
        <span className={"badge badge--accent"} title={termTypeLabel(hit.term_type)}>
          {termTypeCode(hit.term_type)}
        </span>
        <span className="badge badge--muted">{domainLabel(hit.product_area)}</span>
      </header>
      <div className="exact-hit__meta small muted">
        chunk <span className="mono">{shortId(hit.chunk_id)}</span>
        {" · "}doc <span className="mono">{shortId(hit.document_id)}</span>
        {" · "}score <span className="mono">{hit.score.toFixed(3)}</span>
      </div>
    </article>
  );
}