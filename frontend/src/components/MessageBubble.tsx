/**
 * One message in the chat — either a user question or an
 * assistant response.
 *
 * Renders four assistant-side states:
 *   1. in-flight: the typing indicator
 *   2. grounded:  answer prose + citation markers + citation list
 *   3. unsupported / no-answer: muted card with the canned
 *      refusal copy and a chip explaining why
 *   4. error:     error envelope + retry button
 *
 * User messages are just prose in a coloured bubble.
 *
 * The bubble deliberately leaves the answer text as plain text
 * (no markdown rendering) because the stub LLM returns plain
 * text. V1's real Anthropic client will return markdown, and
 * that's the moment a `react-markdown` step plugs in here.
 */

import type { AskResponse, Confidence, Domain, Intent, RetrievalStrategy } from "../lib/types";
import { confidenceLabel, domainLabel, intentLabel, strategyLabel } from "../lib/format";
import { CitationList, CitationMarkers } from "./Citations";
import { TypingIndicator } from "./TypingIndicator";

// ---------------------------------------------------------------------------
// User message
// ---------------------------------------------------------------------------

interface UserMessageProps {
  text: string;
}

export function UserMessage({ text }: UserMessageProps) {
  return (
    <div className="bubble-row bubble-row--user">
      <div className="bubble bubble--user">
        <p className="bubble__text">{text}</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Assistant message
// ---------------------------------------------------------------------------

export type AssistantMessageState =
  | { kind: "in-flight" }
  | { kind: "grounded"; response: AskResponse }
  | { kind: "refused"; response: AskResponse; reason: "unsupported" | "no_answer" }
  | { kind: "error"; message: string; onRetry?: () => void };

interface AssistantMessageProps {
  state: AssistantMessageState;
}

export function AssistantMessage({ state }: AssistantMessageProps) {
  if (state.kind === "in-flight") {
    return (
      <div className="bubble-row bubble-row--assistant">
        <div className="bubble bubble--assistant">
          <TypingIndicator />
        </div>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className="bubble-row bubble-row--assistant">
        <div className="bubble bubble--assistant bubble--error">
          <div className="bubble__error">
            <span className="bubble__error-icon" aria-hidden="true">⚠</span>
            <div className="column" style={{ gap: "var(--space-1)" }}>
              <span className="bubble__error-title">Couldn't reach CiteVyn</span>
              <span className="bubble__error-message">{state.message}</span>
            </div>
          </div>
          {state.onRetry && (
            <button type="button" className="button button--secondary button--small" onClick={state.onRetry} data-testid="retry-btn">
              Retry
            </button>
          )}
        </div>
      </div>
    );
  }

  if (state.kind === "refused") {
    return <RefusedBubble response={state.response} reason={state.reason} />;
  }

  return <GroundedBubble response={state.response} />;
}

// ---------------------------------------------------------------------------
// Grounded (the happy path)
// ---------------------------------------------------------------------------

function GroundedBubble({ response }: { response: AskResponse }) {
  return (
    <div className="bubble-row bubble-row--assistant">
      <div className="bubble bubble--assistant">
        <p className="bubble__text bubble__text--prose">{response.answer}</p>

        <ResponseMeta
          domain={response.domain}
          intent={response.intent}
          confidence={response.confidence}
          retrievalStrategy={response.retrieval_strategy}
          cacheHit={response.cache_hit}
        />

        <CitationMarkers citations={response.citations} />
        <CitationList citations={response.citations} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Refused (unsupported / no-answer)
// ---------------------------------------------------------------------------

function RefusedBubble({
  response,
  reason,
}: {
  response: AskResponse;
  reason: "unsupported" | "no_answer";
}) {
  const title = reason === "unsupported" ? "Outside CiteVyn's scope" : "No grounded answer found";
  const icon = reason === "unsupported" ? "🛑" : "✱";

  return (
    <div className="bubble-row bubble-row--assistant">
      <div className="bubble bubble--assistant bubble--muted">
        <div className="bubble__refused-header">
          <span className="bubble__refused-icon" aria-hidden="true">{icon}</span>
          <span className="bubble__refused-title">{title}</span>
          <span className={"badge " + (reason === "unsupported" ? "badge--warning" : "badge--muted")}>
            {intentLabel(response.intent)}
          </span>
        </div>
        <p className="bubble__text bubble__text--prose">{response.answer}</p>
        <ResponseMeta
          domain={response.domain}
          intent={response.intent}
          confidence={response.confidence}
          retrievalStrategy={response.retrieval_strategy}
          cacheHit={response.cache_hit}
          muted
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Metadata strip (domain / intent / confidence / strategy)
// ---------------------------------------------------------------------------

function ResponseMeta({
  domain,
  intent,
  confidence,
  retrievalStrategy,
  cacheHit,
  muted,
}: {
  domain: Domain;
  intent: Intent;
  confidence: Confidence;
  retrievalStrategy: RetrievalStrategy;
  cacheHit: boolean;
  muted?: boolean;
}) {
  return (
    <div className="meta-strip">
      <span className={"badge " + (muted ? "badge--muted" : "badge--accent")}>
        {domainLabel(domain)}
      </span>
      <span className="badge badge--muted">{intentLabel(intent)}</span>
      <span className="badge badge--muted" title={confidenceLabel(confidence)}>
        {confidence}
      </span>
      <span className="badge badge--muted" title="Retrieval strategy">
        {strategyLabel(retrievalStrategy)}
        {cacheHit ? " · cache" : ""}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Re-exports for tests
// ---------------------------------------------------------------------------

/** Exposed for unit tests; not part of the public component API. */
export const __testing__ = { CitationList, CitationMarkers, TypingIndicator };