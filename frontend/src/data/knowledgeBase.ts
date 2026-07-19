/**
 * Knowledge base data module for CiteVyn demo.
 *
 * In production this module is replaced by the real retrieval API
 * (call /v1/sessions/{id}/messages with the user's question).
 * The matchKB() function demonstrates keyword-based routing that
 * will be swapped out for vector search.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Source {
  n: string;
  title: string;
  url: string;
}

export interface KBEntry {
  q: string;
  tag: string;
  a: string;
  sources: Source[];
  refusal?: boolean;
}

// ---------------------------------------------------------------------------
// Knowledge base entries
// ---------------------------------------------------------------------------

export const KB: Record<string, KBEntry> = {
  "claude-code": {
    q: "What is Claude Code?",
    tag: "USAGE",
    a: "Claude Code is Anthropic's agentic coding tool. It reads and edits files across your project, runs commands, and handles git workflows — working inside your existing dev environment. You can use it as a terminal CLI, a desktop app, a web app, or an extension for editors like VS Code and JetBrains.",
    sources: [
      {
        n: "1",
        title: "Claude Code — Overview",
        url: "docs.claude.com/en/docs/claude-code/overview",
      },
      {
        n: "2",
        title: "Quickstart",
        url: "docs.claude.com/en/docs/claude-code/quickstart",
      },
    ],
  },
  "codex-flag": {
    q: "What does the --model flag do in Codex?",
    tag: "EXACT LOOKUP",
    a: "The --model flag (short form -m) sets which model Codex uses for that run, overriding your configured default. Pass a model name after it, e.g. codex -m o4-mini. It applies only to the current invocation.",
    sources: [
      {
        n: "1",
        title: "Codex CLI — Command reference",
        url: "developers.openai.com/codex/cli/reference",
      },
    ],
  },
  "gemini-stream": {
    q: "How do I stream responses from the Gemini API?",
    tag: "HOW-TO",
    a: "Call the streaming variant of the generate-content method and iterate over the chunks as they arrive, rendering each partial response. In most SDKs this is generateContentStream, which returns an async stream of candidate deltas.",
    sources: [
      {
        n: "1",
        title: "Text generation — Streaming",
        url: "ai.google.dev/gemini-api/docs/text-generation",
      },
    ],
  },
  "claude-code-cost": {
    q: "Does Claude Code cost money?",
    tag: "PRICING",
    a: "Claude Code has no separate fee — usage is billed through your Anthropic account, either as API token usage or as part of an eligible Claude subscription plan. The docs recommend a subscription plan for regular day-to-day use.",
    sources: [
      {
        n: "1",
        title: "Claude Code — Manage costs",
        url: "docs.claude.com/en/docs/claude-code/costs",
      },
    ],
  },
  "claude-models": {
    q: "Which Claude models are available in the API?",
    tag: "COMPARE",
    a: "The Claude API offers three model families: Opus for the most complex work, Sonnet for a balance of intelligence and speed, and Haiku for near-instant responses. Each family is versioned, and the docs list current model IDs with their context windows.",
    sources: [
      {
        n: "1",
        title: "Models overview",
        url: "docs.claude.com/en/docs/about-claude/models",
      },
    ],
  },
  "gemini-key": {
    q: "How do I get a Gemini API key?",
    tag: "SETUP",
    a: "Create a free API key in Google AI Studio — sign in, open the API keys page, and click Create API key. Then pass it to the SDK or set it as the GEMINI_API_KEY environment variable.",
    sources: [
      {
        n: "1",
        title: "Gemini API — API keys",
        url: "ai.google.dev/gemini-api/docs/api-key",
      },
    ],
  },
  "codex-install": {
    q: "How do I install the Codex CLI?",
    tag: "SETUP",
    a: "Install it globally with npm: npm install -g @openai/codex, then run codex inside your project directory to start. On first run you sign in with your ChatGPT account or an API key.",
    sources: [
      {
        n: "1",
        title: "Codex CLI — Getting started",
        url: "developers.openai.com/codex/cli",
      },
    ],
  },
  "laptop": {
    q: "What's the best laptop for AI coding?",
    tag: "OUT OF SCOPE",
    refusal: true,
    a: "I can answer questions about Claude, Claude Code, Codex, and Gemini using their official documentation. I don't have credible source material in this assistant to answer that.",
    sources: [],
  },
  // Canned answer for the "Get Pro" pricing CTA. Keyed like any other entry so
  // it routes through the single `send` path (and its one dedup guard) instead
  // of a bespoke handler.
  "pro": {
    q: "What do I get with CiteVyn Pro?",
    tag: "PRICING",
    a: "Pro isn't live yet — CiteVyn is an early demo, and everything here is free to try. Pro will add higher rate limits, exact lookups, saved history, and shareable answers. For now, ask me anything about Claude, Claude Code, Codex, or Gemini.",
    sources: [],
  },
};

// ---------------------------------------------------------------------------
// Demo ordering and marquee data
// ---------------------------------------------------------------------------

/** Questions that cycle in the hero auto-play card */
export const HERO_ORDER = ["claude-code", "gemini-key", "codex-flag"] as const;

/** Questions in the question ticker (infinite marquee) */
export const MARQUEE = [
  "claude-code",
  "gemini-key",
  "codex-flag",
  "claude-code-cost",
  "gemini-stream",
  "codex-install",
  "claude-models",
  "laptop",
] as const;

/** Questions in the interactive demo left rail */
export const DEMO_ORDER = ["claude-code", "codex-flag", "gemini-stream", "laptop"] as const;

/** Rotating placeholder text for the hero input */
export const PLACEHOLDERS = [
  "Ask about Claude, Codex, Gemini…",
  "Does Claude Code cost money?",
  "How do I get a Gemini API key?",
  "What does --model do in Codex?",
  "Which Claude models are available?",
];

/** Fallback answer when no KB entry matches */
export const GENERIC_REFUSAL =
  "I can answer questions about Claude, Claude Code, Codex, and Gemini using their official documentation. I don't have credible source material in this assistant to answer that.";

// ---------------------------------------------------------------------------
// Keyword matcher — routes free-typed questions to a KB entry
// ---------------------------------------------------------------------------

/**
 * Match a free-text question against the canned KB.
 *
 * In production this is replaced by the real retrieval pipeline:
 * 1. POST /v1/sessions  — create a session
 * 2. POST /v1/sessions/{id}/messages  — send the question, get streaming answer
 *
 * @param text - User's free-text question
 * @returns KB entry or generic refusal
 */
export function matchKB(text: string): KBEntry {
  const t = text.toLowerCase();

  // Hard out-of-scope keywords
  if (/laptop|gpu|hardware|weather|stock|recipe/.test(t)) {
    return {
      q: text,
      tag: "OUT OF SCOPE",
      refusal: true,
      a: GENERIC_REFUSAL,
      sources: [],
    };
  }

  // CiteVyn Pro pricing CTA. The "Get Pro" button routes its canned question
  // here; any free-typed question mentioning "citevyn pro" resolves the same
  // way (deliberate — the demo KB now knows about Pro).
  if (t.includes("citevyn pro")) {
    return KB["pro"];
  }

  // Gemini key / setup
  if (t.includes("gemini") && (t.includes("key") || t.includes("get "))) {
    return KB["gemini-key"];
  }

  // Any other Gemini question
  if (t.includes("gemini")) {
    return KB["gemini-stream"];
  }

  // Codex install
  if (t.includes("install") && t.includes("codex")) {
    return KB["codex-install"];
  }

  // Codex flags / --model
  if (t.includes("codex") || t.includes("--") || t.includes("flag")) {
    return KB["codex-flag"];
  }

  // Cost / pricing
  if (
    t.includes("cost") ||
    t.includes("free") ||
    t.includes("money") ||
    t.includes("price")
  ) {
    return KB["claude-code-cost"];
  }

  // Claude models
  if (t.includes("models") || t.includes("which claude")) {
    return KB["claude-models"];
  }

  // Any other Claude question
  if (t.includes("claude")) {
    return KB["claude-code"];
  }

  // Generic refusal
  return {
    q: text,
    tag: "OUT OF SCOPE",
    refusal: true,
    a: GENERIC_REFUSAL,
    sources: [],
  };
}

/**
 * Match questions about CiteVyn *the product itself* (Pro/membership,
 * coverage, trust, freshness).
 *
 * OFFLINE/DEMO FALLBACK ONLY. In live mode the backend now indexes an "About
 * CiteVyn" source (#49), so CiteVyn-meta questions flow through the normal
 * retrieval + citation path and this matcher is NOT consulted. It remains as
 * the demo-mode answer for these questions when there is no backend, so the
 * static landing demo still responds to "What do I get with CiteVyn Pro?".
 *
 * Returns ``null`` when the question is not about CiteVyn itself, so the caller
 * falls through to ``matchKB`` in demo mode.
 *
 * The guard is deliberately narrow — it only fires when the text mentions
 * "citevyn" — so in demo mode genuine product questions ("does Claude Code
 * cost money?") still fall through to ``matchKB``. (In live mode this matcher
 * is never consulted; the caller routes every question to the backend.)
 */
export function matchCitevynMeta(text: string): KBEntry | null {
  const t = text.toLowerCase();
  if (!t.includes("citevyn")) return null;

  const meta = (a: string, tag = "CITEVYN"): KBEntry => ({ q: text, tag, a, sources: [] });

  // Pricing / Pro / membership / plans.
  if (/\bpro\b|member|subscri|plan|pric|cost|money|\bfree\b|upgrade|worth|buy|billing|tier/.test(t)) {
    return KB["pro"];
  }
  // Which tools / coverage.
  if (/cover|support|which tool|what tool|\btools\b|products/.test(t)) {
    return meta(
      "CiteVyn covers Claude (API), Claude Code, OpenAI Codex, and Google Gemini today — using their official documentation only. ChatGPT and Cursor are planned, but not available yet.",
    );
  }
  // Trust / hallucination / accuracy.
  if (/hallucin|guess|accura|trust|reliab|made up|wrong/.test(t)) {
    return meta(
      "CiteVyn is designed not to hallucinate: every answer is grounded in the official docs and links to the exact source page so you can verify it, and if the docs don't support an answer it refuses instead of guessing.",
    );
  }
  // Freshness / index.
  if (/fresh|updat|stale|\bold\b|current|index/.test(t)) {
    return meta(
      "CiteVyn serves from the last known-good index, so a failed re-index never corrupts what's live. Scheduled source refresh is an Enterprise feature.",
    );
  }
  // Generic "what is CiteVyn".
  return meta(
    "CiteVyn gives you cited, checkable answers about your AI tools (Claude, Claude Code, Codex, Gemini) straight from the makers' official docs — every claim links to the exact source page, and it says \"I don't know\" instead of guessing.",
  );
}

// ---------------------------------------------------------------------------
// Pricing tier data
// ---------------------------------------------------------------------------

export interface PricingTier {
  name: string;
  price: string;
  unit: string;
  desc: string;
  cta: string;
  featured: boolean;
  features: string[];
  // name === "Enterprise" → no-op; "Pro" → getPro flow; others → open chat
  action: () => void;
}

// ---------------------------------------------------------------------------
// Persona data
// ---------------------------------------------------------------------------

export interface Persona {
  tag: string;
  title: string;
  body: string;
  qs: Array<{ q: string; select: () => void }>;
}

// ---------------------------------------------------------------------------
// FAQ data
// ---------------------------------------------------------------------------

export interface FAQItem {
  q: string;
  a: string;
}

export const FAQ_DATA: FAQItem[] = [
  {
    q: "Which tools does CiteVyn cover?",
    a: "CiteVyn covers Claude (API), Claude Code, OpenAI Codex, and Google Gemini today — using their official documentation only. ChatGPT and Cursor are planned, but not available yet.",
  },
  {
    q: "How do citations work?",
    a: "Every factual answer is built only from passages of the official docs, and each claim links to the exact source page it came from. If a claim isn't backed by a source, it isn't made.",
  },
  {
    q: "What happens when it can't find an answer?",
    a: "CiteVyn refuses rather than guesses. If the docs don't support a reliable answer, or the question is outside the supported tools, it tells you so plainly instead of hallucinating.",
  },
  {
    q: "Does CiteVyn hallucinate?",
    a: "It's designed not to. Answers are grounded in the official docs and checked by an automated quality suite — targeting 95%+ correct, faithful citations — before each release.",
  },
  {
    q: "Can it answer questions about my private docs?",
    a: "Not yet — today it uses public official documentation only. Connecting your own private docs, single sign-on, and per-team data isolation are planned for Enterprise.",
  },
  {
    q: "How fresh is the documentation?",
    a: "CiteVyn serves from the last known-good index, so a failed re-index never corrupts what's live. Scheduled source refresh is an Enterprise feature.",
  },
];

// ---------------------------------------------------------------------------
// Stat gates and feature cards
// ---------------------------------------------------------------------------

export interface StatGate {
  label: string;
  value: string;
}

export interface FeatureCard {
  mark: string;
  title: string;
  body: string;
}

export const STAT_GATES: StatGate[] = [
  { label: "Citation correctness", value: "≥95%" },
  { label: "Guardrail on critical cases", value: "100%" },
  { label: "Found the right source", value: "≥95%" },
];

export const FEATURE_CARDS: FeatureCard[] = [
  {
    mark: "⌗",
    title: "Citation on every claim",
    body: "No factual sentence without a source you can open and check.",
  },
  {
    mark: "⏻",
    title: "Refuses out-of-scope",
    body: "Ask about anything but the four tools and it declines — cleanly.",
  },
  {
    mark: "⏗",
    title: "Exact lookup",
    body: "Flags, commands, model names, config keys and errors, matched precisely.",
  },
  {
    mark: "↰",
    title: "Clean follow-ups",
    body: "Switch tools mid-session without context bleeding across products.",
  },
];
