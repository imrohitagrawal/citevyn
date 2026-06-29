/**
 * Three-dot typing indicator. Used in place of the assistant
 * bubble while a request is in flight.
 *
 * Pure CSS animation — no JS timer, no spring library. The dots
 * fade up and down with staggered delays; the wrapper itself has
 * a subtle pulse on its container so the whole element feels
 * alive without being distracting.
 */
export function TypingIndicator() {
  return (
    <div className="typing-indicator" aria-label="Generating answer" role="status">
      <span className="typing-indicator__dot" />
      <span className="typing-indicator__dot" />
      <span className="typing-indicator__dot" />
    </div>
  );
}