/**
 * Feedback under a live answer (ADR-0005 §6 trust must-haves).
 *
 * - "Helpful" is a toggle button (`aria-pressed`): pressing it saves a thumbs up.
 * - "Report a problem" is a disclosure button (`aria-expanded` only; it saves
 *   nothing by itself). It opens "report wrong answer": a reason and an optional
 *   comment, sent only when the reader submits. After a report, a visible line
 *   says so. A later "Helpful" replaces the report (one vote per answer).
 * - "Request this source" is offered only when the answer was refused for want of
 *   a source (not for an out-of-scope question): it logs the question, and an
 *   optional link and note, to the gap log.
 *
 * Loaded lazily from ChatView, so the eager bundle carries none of this. Results
 * are announced in an aria-live region scoped to this answer (not role="status":
 * the chat owns the page's one status region, #356). After a successful submit,
 * focus returns to the button that opened the form.
 *
 * Input rules (TEST_STRATEGY §8b): every text field is optional, so an empty or
 * whitespace-only field simply means "not given". Length is checked at SUBMIT,
 * counted the server's way, refused with an announcement, and the text is kept;
 * no length attribute clamps a paste. The link is checked here too (the form is
 * `noValidate`, so the browser's own popup never replaces our announced message).
 * One request at a time: a submit while one is in flight is refused and
 * announced, which also makes a double-submit send once.
 *
 * "Share" (ADR-0005 Phase 6C-2, access model on, answers with sources only)
 * makes a public, frozen copy of this question and answer, copies its link, and
 * says who can see it and how to stop sharing it.
 */
import { useRef, useState } from "react";
import { apiFetch } from "../lib/api";
import { ApiClientError } from "../lib/types";
import { isSubmitKey, questionLength } from "../lib/composerInput";
import { MAX_FEEDBACK_COMMENT, MAX_SOURCE_NOTE, MAX_SOURCE_URL } from "../lib/feedbackLimits";
import { useClientConfig } from "../lib/clientConfig";
import { shareAnswer, shareLink } from "../lib/shares";

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
  /** Offer "Request this source": the answer was refused for want of a source. */
  offerSourceRequest?: boolean;
  /** An answer with sources: it can be shared (with the access model on). */
  shareable?: boolean;
}

export default function AnswerActions({
  sessionId,
  messageId,
  question,
  offerSourceRequest,
  shareable,
}: Props) {
  const accessModel = useClientConfig().access_model; // a hook: never behind a condition
  const canShare = !!shareable && accessModel;
  const [shared, setShared] = useState<string | null>(null);
  const [rating, setRating] = useState<"up" | "down" | null>(null);
  const [panel, setPanel] = useState<"report" | "source" | null>(null);
  const [reason, setReason] = useState("wrong");
  // Separate drafts: one shared field leaked a report comment into the gap log.
  const [comment, setComment] = useState("");
  const [note, setNote] = useState("");
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState("");
  // A ref, not state: two clicks in one tick both read the state from before the
  // first one ran, so a state flag would let both requests through.
  const busy = useRef(false);
  const reportBtn = useRef<HTMLButtonElement>(null);
  const sourceBtn = useRef<HTMLButtonElement>(null);

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
    } catch (err) {
      const code = err instanceof ApiClientError ? err.status : 0;
      // A 422 is about THIS input and will fail the same way again, so it must not
      // say "try again"; a 401 means the session is gone.
      setStatus(
        code === 422
          ? "That was not accepted. Please check what you entered."
          : code === 401
            ? "Please sign in again to send feedback."
            : "Could not save that. Please try again.",
      );
      return false;
    } finally {
      busy.current = false;
    }
  };

  const feedbackPath = `/v1/sessions/${sessionId}/messages/${messageId}/feedback`;

  const voteUp = async () => {
    if (await send(feedbackPath, "PUT", { rating: "up" }, "Thanks for the feedback.")) {
      setRating("up");
      setPanel(null);
    }
  };

  const sendReport = async () => {
    if (tooLong(comment, MAX_FEEDBACK_COMMENT, "comment")) return;
    const body = { rating: "down", reason, ...(comment.trim() ? { comment: comment.trim() } : {}) };
    if (await send(feedbackPath, "PUT", body, "Thanks, we logged your report.")) {
      setRating("down");
      setPanel(null);
      setComment("");
      reportBtn.current?.focus();
    }
  };

  const share = async () => {
    if (busy.current) { // one request at a time, shared with feedback
      setStatus("Still sending. Please wait a moment.");
      return;
    }
    busy.current = true;
    setStatus("");
    try {
      const link = shareLink(await shareAnswer(sessionId, messageId), window.location.origin);
      if (!link) throw new Error("unexpected share link");
      setShared(link);
      const copied = await navigator.clipboard?.writeText(link).then(
        () => true,
        () => false,
      );
      setStatus(copied ? "Link copied." : "Link ready. Copy it below.");
    } catch (err) {
      const e = err instanceof ApiClientError ? err : null;
      setStatus(
        e?.errorCode() === "plan_required"
          ? "Sharing answers is part of Pro."
          : e?.status === 401
            ? "Please sign in to share."
            : "Could not share that. Please try again.",
      );
    } finally {
      busy.current = false;
    }
  };

  const sendSourceRequest = async () => {
    const link = url.trim();
    if (link && !/^https?:\/\/\S+$/i.test(link)) {
      setStatus("The link must start with http:// or https://.");
      return;
    }
    if (tooLong(link, MAX_SOURCE_URL, "link") || tooLong(note, MAX_SOURCE_NOTE, "note")) return;
    const body = {
      question,
      message_id: messageId,
      ...(link ? { requested_url: link } : {}),
      ...(note.trim() ? { note: note.trim() } : {}),
    };
    if (await send("/v1/source-requests", "POST", body, "Thanks, we logged your request.")) {
      setPanel(null);
      setUrl("");
      setNote("");
      sourceBtn.current?.focus();
    }
  };

  return (
    <div className="answer-actions">
      <div className="answer-actions-row">
        <button type="button" className="answer-action" aria-pressed={rating === "up"} onClick={voteUp}>
          Helpful
        </button>
        <button
          ref={reportBtn}
          type="button"
          className="answer-action"
          aria-expanded={panel === "report"}
          onClick={() => setPanel(panel === "report" ? null : "report")}
        >
          Report a problem
        </button>
        {offerSourceRequest && (
          <button
            ref={sourceBtn}
            type="button"
            className="answer-action"
            aria-expanded={panel === "source"}
            onClick={() => setPanel(panel === "source" ? null : "source")}
          >
            Request this source
          </button>
        )}
        {canShare && (
          <button type="button" className="answer-action" onClick={() => void share()}>
            Share
          </button>
        )}
      </div>
      {shared && (
        <p className="answer-actions-note" data-testid="share-link">
          Anyone with this link can see this question and answer:{" "}
          <a href={shared} target="_blank" rel="noopener noreferrer">
            {shared}
          </a>
          . Deleting this chat does not remove it; stop sharing it under Shared links in your
          account menu.
        </p>
      )}
      {rating === "down" && <p className="answer-actions-note">You reported this answer.</p>}

      {panel === "report" && (
        <form
          className="answer-form"
          noValidate
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
            <textarea value={comment} onChange={(e) => setComment(e.target.value)} />
          </label>
          <button type="submit" className="answer-action">
            Send report
          </button>
        </form>
      )}

      {panel === "source" && (
        <form
          className="answer-form"
          noValidate
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
            <textarea value={note} onChange={(e) => setNote(e.target.value)} />
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
