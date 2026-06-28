/**
 * First-load hero with example prompts.
 *
 * Renders a brief product pitch and a row of clickable chips
 * that pre-fill the chat input. The chips are deliberately
 * short — a reviewer can read each in a second and pick the
 * one that appeals.
 *
 * The first three are the demos that work against the seeded
 * catalog; the fourth ("What is the capital of France?")
 * deliberately probes the guardrail so the reviewer sees the
 * "outside scope" empty state.
 */

interface ExamplePromptsProps {
  onSelect: (prompt: string) => void;
}

interface Example {
  id: string;
  prompt: string;
  description: string;
}

const EXAMPLES: ReadonlyArray<Example> = [
  {
    id: "rate-limit",
    prompt: "What is the default rate limit for the Claude API?",
    description: "Supported · Claude API",
  },
  {
    id: "permissions",
    prompt: "How do I configure Claude Code permissions?",
    description: "Supported · Claude Code",
  },
  {
    id: "exact-flag",
    prompt: "What does the --max-tokens flag do in Codex?",
    description: "Supported · Codex",
  },
  {
    id: "out-of-scope",
    prompt: "What is the capital of France?",
    description: "Outside scope · shows refusal",
  },
];

export function ExamplePrompts({ onSelect }: ExamplePromptsProps) {
  return (
    <div className="hero">
      <div className="hero__icon" aria-hidden="true">
        <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="10" />
          <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
      </div>
      <h3 className="hero__title">
        Ask anything. <span className="landing__highlight landing__highlight--inline">
          <span className="landing__highlight-bar" aria-hidden="true" />
          <span className="landing__highlight-text">Get cited.</span>
        </span>
      </h3>
      <p className="hero__subtitle">
        Four quick demos against the seeded catalog. The last one deliberately
        probes the guardrail so you can see the "outside scope" state.
      </p>

      <div className="hero__examples" role="list">
        {EXAMPLES.map((ex) => (
          <button
            key={ex.id}
            type="button"
            className="hero__example"
            onClick={() => onSelect(ex.prompt)}
            role="listitem"
            data-testid={`example-prompt-${ex.id}`}
          >
            <span className="hero__example-prompt">{ex.prompt}</span>
            <span className="hero__example-meta">{ex.description}</span>
          </button>
        ))}
      </div>
    </div>
  );
}