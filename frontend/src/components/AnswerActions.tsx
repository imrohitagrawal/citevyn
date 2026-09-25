/**
 * Feedback under a live answer (ADR-0005 §6 trust must-haves).
 *
 * - "Helpful" / "Not helpful" (thumbs up/down), exposed with `aria-pressed`.
 * - "Not helpful" opens "report wrong answer": a reason and an optional comment.
 *   Nothing is sent until the reader submits, so a mis-click costs nothing.
 * - On a refusal, "Request this source" logs the question (and an optional link)
 *   to the gap log.
 *
 * Loaded lazily from ChatView, so the eager bundle carries none of this.
 *
 * Input rules (TEST_STRATEGY §8b): every text field is optional, so an empty or
 * whitespace-only field simply means "not given". Length is checked at SUBMIT,
 * counted the server's way, refused with an announcement, and the text is kept;
 * no length attribute clamps a paste. One request at a time: a submit while one
 * is in flight is refused and announced, which also makes a double-submit send
 * once.
 * Results are announced in an aria-live region scoped to this answer (not
 * role="status": the chat owns the page's one status region, #356).
 */
import { useRef, useState } from "react";
import { apiFetch } from "../lib/api";
import { isSubmitKey, questionLength } from "../lib/composerInput";
import { MAX_FEEDBACK_COMMENT, MAX_SOURCE_NOTE, MAX_SOURCE_URL } from "../lib/feedbackLimits";

const REASONS: Array<[string, string]> = [
  ["wrong", "Wrong"],
  ["outdated", "Outdated"],
  ["missing_source", "Source missing"],
  ["unclear", "Unclear"],
  ["other", "Other"],
];

interface Props {
  sessionId: string;
  messageId: string;
  /** The reader's question, sent with a source request. */
  question: string;
  refusal?: boolean;
}

export default function AnswerActions({ sessionId, messageId, question, refusal }: Props) {
  const [rating, setRating] = useState<"up" | "down" | null>(null);
  const [panel, setPanel] = useState<"report" | "source" | null>(null);
  const [reason, setReason] = useState("wrong");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState("");
  // A ref, not state: two clicks in one tick both read the state from before the
  // first one ran, so a state flag would let both requests through.
  const busy = useRef(false);

  /** Refuse (and announce) a field over its limit; keep the text. */
  const tooLong = (value: string, max: number, name: string) => {
    const n = questionLength(value.trim());
    if (n <= max) return false;
    setStatus(`The ${name} is too long: ${n} of ${max} characters. Please shorten it.`);
    return true;
  };

  const send = async (path: string, method: string, body: object, done: string) => {
    if (busy.current) {
      setStatus("Still sending. Please wait a moment.");
      return false;
    }
    busy.current = true;
    setStatus("");
    try {
      await apiFetch(path, { method, body: JSON.stringify(body) });
      setStatus(done);
      return true;
    } catch {
      setStatus("Could not save that. Please try again.");
      return false;
    } finally {
      busy.current = false;
    }
  };

  const feedbackPath = `/v1/sessions/${sessionId}/messages/${messageId}/feedback`;

  const voteUp = async () => {
    setPanel(null);
    if (await send(feedbackPath, "PUT", { rating: "up" }, "Thanks for the feedback.")) {
      setRating("up");
    }
  };

  const sendReport = async () => {
    if (tooLong(text, MAX_FEEDBACK_COMMENT, "comment")) return;
    const body = { rating: "down", reason, ...(text.trim() ? { comment: text.trim() } : {}) };
    if (await send(feedbackPath, "PUT", body, "Thanks, we logged your report.")) {
      setRating("down");
      setPanel(null);
      setText("");
    }
  };

  const sendSourceRequest = async () => {
    if (tooLong(url, MAX_SOURCE_URL, "link") || tooLong(text, MAX_SOURCE_NOTE, "note")) return;
    const body = {
      question,
      message_id: messageId,
      ...(url.trim() ? { requested_url: url.trim() } : {}),
      ...(text.trim() ? { note: text.trim() } : {}),
    };
    if (await send("/v1/source-requests", "POST", body, "Thanks, we logged your request.")) {
      setPanel(null);
      setUrl("");
      setText("");
    }
  };

  return (
    <div className="answer-actions">
      <div className="answer-actions-row">
        <button type="button" className="answer-action" aria-pressed={rating === "up"} onClick={voteUp}>
          Helpful
        </button>
        <button
          type="button"
          className="answer-action"
          aria-pressed={rating === "down"}
          aria-expanded={panel === "report"}
          onClick={() => setPanel(panel === "report" ? null : "report")}
        >
          Not helpful
        </button>
        {refusal && (
          <button
            type="button"
            className="answer-action"
            aria-expanded={panel === "source"}
            onClick={() => setPanel(panel === "source" ? null : "source")}
          >
            Request this source
          </button>
        )}
      </div>

      {panel === "report" && (
        <form
          className="answer-form"
          onSubmit={(e) => {
            e.preventDefault();
            void sendReport();
          }}
        >
          <fieldset>
            <legend>What was wrong with this answer?</legend>
            {REASONS.map(([value, label]) => (
              <label key={value} className="answer-form-choice">
                <input
                  type="radio"
                  name={`reason-${messageId}`}
                  value={value}
                  checked={reason === value}
                  onChange={() => setReason(value)}
                />
                {label}
              </label>
            ))}
          </fieldset>
          <label className="answer-form-field">
            What is wrong? (optional)
            <textarea value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="submit" className="answer-action">
            Send report
          </button>
        </form>
      )}

      {panel === "source" && (
        <form
          className="answer-form"
          onSubmit={(e) => {
            e.preventDefault();
            void sendSourceRequest();
          }}
        >
          <label className="answer-form-field">
            Link to the page (optional)
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              // Enter in a text field submits its form. Not while an IME is
              // composing: that Enter picks a candidate (§8b, isSubmitKey).
              onKeyDown={(e) => {
                if (e.key === "Enter" && !isSubmitKey(e)) e.preventDefault();
              }}
            />
          </label>
          <label className="answer-form-field">
            Anything else? (optional)
            <textarea value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="submit" className="answer-action">
            Send request
          </button>
        </form>
      )}

      <span aria-live="polite" className="answer-actions-status" data-testid="answer-actions-status">
        {status}
      </span>
    </div>
  );
}
