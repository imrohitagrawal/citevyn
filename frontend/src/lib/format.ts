/**
 * Display formatters for the CiteVyn UI.
 *
 * These helpers are pure and side-effect free. They take the raw
 * enum values the backend returns and produce strings the UI can
 * render directly. The mapping tables live here, not in the
 * components, so a backend rename only touches this file.
 */

import type { Confidence, Domain, Intent, RetrievalStrategy, TermType } from "./types";

// ---------------------------------------------------------------------------
// Domain and intent labels
// ---------------------------------------------------------------------------

/**
 * Human-friendly domain label, used in the citation list ("Source:
 * Claude API") and in the metadata chips. Keep this in sync with
 * :class:`app.guardrails.domain.Domain` in the backend.
 */
const DOMAIN_LABELS: Record<Domain, string> = {
  claude_api: "Claude API",
  claude_code: "Claude Code",
  codex: "Codex",
  gemini_api: "Gemini",
  citevyn: "About CiteVyn",
  unsupported: "Outside scope",
};

export function domainLabel(domain: Domain): string {
  return DOMAIN_LABELS[domain] ?? domain;
}

const INTENT_LABELS: Record<Intent, string> = {
  faq: "FAQ",
  how_to: "How-to",
  troubleshooting: "Troubleshooting",
  lookup: "Lookup",
  smalltalk: "Small talk",
  unsupported: "Unsupported",
};

export function intentLabel(intent: Intent): string {
  return INTENT_LABELS[intent] ?? intent;
}

// ---------------------------------------------------------------------------
// Confidence, strategy, term type
// ---------------------------------------------------------------------------

const CONFIDENCE_LABELS: Record<Confidence, string> = {
  none: "No confidence",
  low: "Low confidence",
  medium: "Medium confidence",
  high: "High confidence",
};

export function confidenceLabel(confidence: Confidence): string {
  return CONFIDENCE_LABELS[confidence] ?? confidence;
}

const STRATEGY_LABELS: Record<RetrievalStrategy, string> = {
  none: "no retrieval",
  cache: "cache hit",
  exact_lookup: "exact lookup",
  hybrid_reranked: "hybrid + reranked",
};

export function strategyLabel(strategy: RetrievalStrategy): string {
  return STRATEGY_LABELS[strategy] ?? strategy;
}

/**
 * Human label + short code per term type. The ``code`` is what
 * renders in the badge (``flag``, ``env_var``); the ``label`` is
 * used for ``aria-label`` and tooltips so screen readers don't
 * hear the underscore.
 */
/**
 * Per-term-type display metadata. ``code`` is the short token that
 * renders in the badge; ``label`` is the long form for screen
 * readers and tooltips.
 */
const TERM_TYPE_META: Record<TermType, { code: string; label: string }> = {
  flag: { code: "flag", label: "CLI flag" },
  command: { code: "cmd", label: "Command" },
  config_key: { code: "config", label: "Configuration key" },
  model_name: { code: "model", label: "Model name" },
  api_parameter: { code: "param", label: "API parameter" },
  error_message: { code: "error", label: "Error message" },
  environment_variable: { code: "env", label: "Environment variable" },
  file_name: { code: "file", label: "File name" },
  slash_command: { code: "slash", label: "Slash command" },
};

export function termTypeCode(type: TermType): string {
  return TERM_TYPE_META[type]?.code ?? type;
}

export function termTypeLabel(type: TermType): string {
  return TERM_TYPE_META[type]?.label ?? type;
}

// ---------------------------------------------------------------------------
// Time helpers
// ---------------------------------------------------------------------------

/**
 * "Just now", "2m ago", "1h ago", "yesterday" — used in the
 * sidebar session list. We keep the bucket boundaries tight (60s,
 * 60m, 24h) because a session list more than a day old is
 * unusual in a demo.
 */
export function relativeTime(iso: string | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diffMs = Date.now() - then;
  const diffSec = Math.max(0, Math.round(diffMs / 1000));
  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  if (diffDay === 1) return "yesterday";
  if (diffDay < 7) return `${diffDay}d ago`;
  return new Date(iso).toLocaleDateString();
}

// ---------------------------------------------------------------------------
// Identifiers
// ---------------------------------------------------------------------------

/**
 * Render a UUID-ish string in short form for the UI
 * (``"a1b2c3d4…"``). The backend returns full UUIDs everywhere;
 * the UI only needs enough to be unique within a session.
 */
export function shortId(id: string | undefined, length = 8): string {
  if (!id) return "—";
  return id.length <= length ? id : id.slice(0, length);
}
