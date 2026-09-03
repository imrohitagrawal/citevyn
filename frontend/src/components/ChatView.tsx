/**
 * ChatView — Full-screen chat. Renders live answers from the official docs, or
 * sample answers in demo mode.
 */

import { useEffect, useLayoutEffect, useRef } from "react";
import { AnswerBody, hasCitationChips } from "./AnswerBody";
import { isSafeHref } from "../lib/safeHref";

interface ChatViewProps {
  messages: Array<{
    isUser: boolean;
    domId: string;
    userStyle: React.CSSProperties;
    text: string;
    streaming?: boolean;
    refusal?: boolean;
    /** A transport failure (rate limit / server / network) — distinct from a content
     *  refusal, so it shows a rate-limit / connection notice, not "NO SOURCE — REFUSED" (#120). */
    errorKind?: "rate_limit" | "error";
    hasSources?: boolean;
    sources?: Array<{ n: string; title: string; url: string }>;
    /** Nearest-doc suggestions on a graceful fallback (Phase 4a). */
    docSuggestions?: Array<{ title: string; url: string; product_area: string }>;
  }>;
  chatEmpty: boolean;
  chatSuggestions: Array<{ q: string; select: () => void }>;
  chatInput: string;
  onChatInput: (e: React.ChangeEvent<HTMLInputElement>) => void;
  onChatKey: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onSendClick: () => void;
  onBackClick: () => void;
  /** When true the chat is wired to the real backend, not canned answers. */
  live?: boolean;
  /** True while waiting for the backend's first chunk. Renders a "thinking…"
      loader so the user knows the request is in flight. */
  pending?: boolean;
  /** Index of the message bubble currently highlighted (e.g. by the
      duplicate-question "jump-to-existing" feature). */
  highlightedIndex?: number;
  /** Monotonic counter bumped by the hook on every NEW question submit. A change
      here force-scrolls the just-asked question into view even if the reader had
      scrolled up — an explicit send must always be followed. */
  sendTick?: number;
}

export function ChatView({
  messages,
  chatEmpty,
  chatSuggestions,
  chatInput,
  onChatInput,
  onChatKey,
  onSendClick,
  onBackClick,
  live = false,
  pending = false,
  highlightedIndex = -1,
  sendTick = 0,
}: ChatViewProps) {
  const chatListRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLInputElement>(null);
  // Stick-to-bottom LATCH. Armed (true) means "keep pinning to the bottom as new
  // content streams in"; the first time the user scrolls UP it disarms, so streaming
  // chunks stop yanking them back down. It re-arms only when they return to the true
  // bottom. This replaces the old 120px "slack band": that band re-pinned on every
  // streamed token whenever the user was within 120px of the bottom, so an upward
  // scroll of a few px was instantly reversed by the next chunk — the jitter (#122).
  const stickRef = useRef(true);
  // True while a duplicate-question highlight owns the list (see the layout effect
  // below). It blocks only the RE-ARMING of the latch, never its disarming, so a
  // reader who scrolls away mid-flash still takes control.
  const highlightHoldRef = useRef(false);

  // Keep the latch in sync with the user's manual scrolling. A gesture that leaves
  // the true bottom (>8px) disarms; returning to it re-arms. The effect's own
  // programmatic ``scrollTop = scrollHeight`` lands at the bottom, so it keeps the
  // latch armed (correct) rather than fighting itself.
  useEffect(() => {
    const list = chatListRef.current;
    if (!list) return;
    const onScroll = () => {
      const distanceFromBottom = list.scrollHeight - list.scrollTop - list.clientHeight;
      const atBottom = distanceFromBottom <= 8;
      // Two transitions, deliberately NOT symmetric:
      // Leaving the bottom ALWAYS disarms — a reader who scrolls away mid-flash
      // takes control immediately. It also RELEASES the hold: the hold exists only
      // to survive the first frames of a smooth scroll that has not moved yet, so
      // once the list has genuinely left the bottom that window is over. Without
      // this release the hold outlives its purpose, and because re-arming is
      // edge-triggered on a scroll event, a reader who scrolls back to the bottom
      // during the 2s highlight never re-arms — streaming silently stops following
      // even after the highlight has cleared.
      if (!atBottom) {
        stickRef.current = false;
        highlightHoldRef.current = false;
        return;
      }
      // Arriving at the bottom normally re-arms — but not while a highlight owns
      // the list. A SMOOTH scroll away from the bottom starts AT the bottom, so its
      // first frames still read "at bottom" and would re-arm the latch we just
      // disarmed to run them — after which the passive effect pins straight back
      // down and cancels the animation. That loop is what left a landing re-ask
      // stranded at the newest message instead of the answer it asked for (#302).
      if (highlightHoldRef.current) return;
      stickRef.current = true;
    };
    list.addEventListener("scroll", onScroll, { passive: true });
    return () => list.removeEventListener("scroll", onScroll);
  }, []);

  // Passive updates (new message OR a streamed chunk) pin to the bottom ONLY while the
  // latch is armed, so a reader who scrolled up keeps their position.
  useEffect(() => {
    const list = chatListRef.current;
    if (!list) return;
    if (stickRef.current) {
      list.scrollTop = list.scrollHeight;
    }
  }, [messages]);

  // An EXPLICIT send always brings the new question into view, even from a scrolled-up
  // position, and re-arms the latch so its streaming answer stays followed. Keyed on
  // ``sendTick`` (bumped once per submit) so it never runs on a passive stream token.
  //
  // Seeded from the CURRENT value rather than guarded with ``sendTick === 0``, so it
  // fires on a CHANGE and never on mount. ``sendTick`` lives in the landing hook's
  // reducer, which outlives this component: after any earlier send, a remount (every
  // landing -> chat switch) arrived with a non-zero tick, so the old guard let this
  // effect pin to the bottom and RE-ARM the latch immediately after the highlight
  // effect had scrolled away and disarmed it — reinstating #302 on exactly the
  // mount-with-a-pending-highlight path this fix exists to close.
  const seenSendTickRef = useRef(sendTick);
  useEffect(() => {
    if (sendTick === seenSendTickRef.current) return;
    seenSendTickRef.current = sendTick;
    const list = chatListRef.current;
    if (!list) return;
    stickRef.current = true;
    list.scrollTop = list.scrollHeight;
  }, [sendTick]);

  // The bubble a duplicate-question highlight points at. Derived as a STRING so
  // this drives the effect below: ``messages`` is a fresh array identity on every
  // render of the landing hook, so depending on the array itself would re-issue
  // the scroll ~40x/second and restart the smooth animation on every frame.
  const highlightedDomId =
    highlightedIndex >= 0 ? messages[highlightedIndex]?.domId : undefined;

  // The legend ("Numbers link each sentence to its source below.") is a one-time
  // orientation, so it goes under the FIRST answer that actually renders chips.
  // Demo-mode answers carry sources but no ``[n]`` markers, so they produce no
  // chips and no legend — which is correct: there would be nothing to explain.
  const legendIndex = messages.findIndex(
    (m) => !m.isUser && m.hasSources && hasCitationChips(m.text, m.sources ?? []),
  );

  // A duplicate-question highlight OWNS the scroll while it is active (#302).
  //
  // This used to be done imperatively from the landing hook, which reached into
  // the DOM by id and returned silently when the bubble was not there yet. The
  // landing entry points switch screen and submit in one gesture, so that lookup
  // raced this component's mount. Reacting to the highlight instead means the
  // scroll happens wherever the mount lands.
  //
  // It must be a LAYOUT effect, for effect-PHASE ordering — not to paint sooner
  // (the scroll is smooth, so nothing is positioned before paint either way).
  // Every layout effect in a commit runs before every passive effect, which is what
  // puts ``stickRef.current = false`` in front of the passive ``[messages]`` pin
  // above. As a plain ``useEffect`` declaration order wins instead, the pin runs
  // first, and the fight this whole fix exists to end resumes inside one commit.
  //
  // Disarming the stick-to-bottom latch is the load-bearing half. Without it the
  // passive effect above re-pins the list to the bottom on the very next render
  // — and the landing hero's demo animation re-renders continuously — so the
  // reader was dragged straight back down off the answer they asked to see.
  useLayoutEffect(() => {
    if (!highlightedDomId) return;
    const el = document.getElementById(highlightedDomId);
    if (!el) return;
    // A reader who asked for less motion gets the jump, not the journey. An explicit
    // ``behavior`` overrides the CSS ``scroll-behavior`` cascade, so the stylesheet
    // cannot do this for us — ``reset.css``'s ``html { scroll-behavior: auto }``
    // targets the document, not this list, and loses to an explicit value anyway.
    //
    // ``matchMedia?.`` is load-bearing, not defensive: jsdom does not implement it,
    // so an unguarded call throws in every unit test. (``useRevealOnScroll`` calls it
    // unguarded, but only from an effect no unit test mounts.)
    const behavior: ScrollBehavior = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches
      ? "auto"
      : "smooth";
    stickRef.current = false;
    highlightHoldRef.current = true;
    const list = chatListRef.current;
    if (!list || list.clientHeight === 0) {
      // No list (or a zero-height one mid-remount): fall back to the browser's own
      // scrolling so the bubble is at least brought into view.
      el.scrollIntoView({ block: "start", behavior });
    } else {
      // The element's top in the list's UNSCROLLED coordinate system — the correct
      // ``scrollTop`` to pin it to the list's visible top.
      //
      // The clamp is defence, not arithmetic: for any descendant of the list this
      // sum is >= -12 (the inset), so the only real negative is the FIRST message,
      // and a browser would clamp a negative ``top`` itself. It earns its place by
      // refusing to turn a meaningless number into a plausible-looking "scrolled to
      // the very top" if ``el`` ever stops being a descendant of ``list``.
      const desiredTop =
        el.getBoundingClientRect().top -
        list.getBoundingClientRect().top +
        list.scrollTop -
        12;
      list.scrollTo({ top: Math.max(0, desiredTop), behavior });
    }
    // Released when the highlight clears (or moves to another bubble), by which
    // time the smooth scroll has settled and the reader is parked on their answer.
    // A flag this effect sets is this effect's to release.
    //
    // HYGIENE, NOT A GUARDED BEHAVIOUR: no test goes red if this release alone is
    // deleted — verified by mutation, not assumed. Holding the flag forever only
    // blocks the latch from RE-arming at the bottom, and every explicit send
    // re-arms it directly via ``sendTick``, so within one mount nothing reachable
    // tells the two apart. The asymmetry it guards IS tested, both directions.
    return () => {
      highlightHoldRef.current = false;
    };
  }, [highlightedDomId]);

  // Entering the chat hands the conversation over to the composer: once a
  // conversation exists the landing sections are no longer a second place to
  // ask, so the chat opens ready to type (#302).
  useEffect(() => {
    // ``preventScroll``: focusing an input scrolls its nearest scrollable ancestor
    // into view, which would compete with the highlight scroll started one effect
    // phase earlier.
    composerRef.current?.focus({ preventScroll: true });
  }, []);

  return (
    <main data-screen-label="Chat">
      <div className="chat-header">
        <button onClick={onBackClick} className="back-button">
          ← Back to landing
        </button>
        <span className="demo-badge">
          {live ? "LIVE — answering from the docs" : "DEMO — sample answers"}
        </span>
      </div>

      <div ref={chatListRef} id="chat-list" className="chat-container">
        {chatEmpty ? (
          // Empty state
          <div className="empty-state">
            <div className="logo-avatar">CV</div>
            <h2>Ask about your AI tools</h2>
            <p>Claude, Claude Code, Codex, and Gemini — answered from official docs, with citations.</p>
            <div className="suggestions">
              {chatSuggestions.map((s, i) => (
                <button key={i} onClick={s.select} className="suggestion-btn">
                  {s.q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          // Messages
          <>
            {messages.map((m, i) => (
              <div
                key={m.domId}
                id={m.domId}
                className={
                  m.isUser
                    ? `message user user-msg${highlightedIndex === i ? " highlighted" : ""}`
                    : `message bot bot-msg${highlightedIndex === i ? " highlighted" : ""}`
                }
                style={m.isUser ? m.userStyle : undefined}
              >
                {!m.isUser && (
                  <div className="avatar bot-avatar">
                    CV
                  </div>
                )}
                <div className="content">
                  {/* A transport failure (429 / server / network) gets its own notice —
                      NOT the content-refusal badge, which means "the corpus had no
                      answer" and must stay reserved for a genuine no_answer (#120). */}
                  {m.errorKind === "rate_limit" && (
                    <div className="notice-badge notice-rate-limit">⏳ ONE MOMENT</div>
                  )}
                  {m.errorKind === "error" && (
                    <div className="notice-badge notice-error">⚠ TEMPORARILY UNAVAILABLE</div>
                  )}
                  {!m.errorKind && m.refusal && (
                    <div className="refusal-badge">
                      ⚠ NO SOURCE — REFUSED
                    </div>
                  )}
                  {/* ``message-body`` is the stable hook for the answer text.
                      Before it existed this div carried NO class when idle, so
                      there was nothing for `white-space: pre-wrap` to attach to
                      — which is how that rule was lost in the landing-design
                      port (2503dd4) and never noticed.

                      A USER bubble is the reader's own text and gets none of the
                      answer formatting: no markdown, no citation chips. */}
                  {m.isUser ? (
                    <div className="message-body">{m.text}</div>
                  ) : (
                    <AnswerBody
                      text={m.text}
                      streaming={m.streaming}
                      sources={m.hasSources ? m.sources || [] : []}
                      showLegend={i === legendIndex}
                    />
                  )}
                  {/* Graceful fallback (Phase 4a): when the answer was declined but the
                      backend found nearby docs, offer them instead of a dead-end refusal.
                      A doc URL is only made clickable when it is a safe http(s)/relative
                      link — a defensive guard so a bad ``javascript:`` URL (were one ever
                      ingested) renders as inert text, not an executable link. */}
                  {m.docSuggestions && m.docSuggestions.length > 0 && (
                    <div className="suggestions" role="group" aria-label="Related documentation">
                      <div className="suggestions-label">You might find these helpful:</div>
                      {m.docSuggestions.map((s) =>
                        isSafeHref(s.url) ? (
                          <a
                            key={s.url + s.title}
                            className="suggestion-card"
                            href={s.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            <span className="suggestion-title">{s.title}</span>
                            <span className="suggestion-url">{s.url}</span>
                          </a>
                        ) : (
                          <div key={s.url + s.title} className="suggestion-card">
                            <span className="suggestion-title">{s.title}</span>
                            <span className="suggestion-url">{s.url}</span>
                          </div>
                        ),
                      )}
                    </div>
                  )}
                </div>
              </div>
            ))}
            {/* Loading indicator while the backend is thinking. Renders as
                its own bot bubble so it scrolls naturally with the rest. */}
            {pending && (
              <div className="message bot bot-msg pending-msg" aria-live="polite">
                <div className="avatar bot-avatar">CV</div>
                <div className="content">
                  <div className="pending-bubble" role="status">
                    <span className="pending-dot" />
                    <span className="pending-dot" />
                    <span className="pending-dot" />
                    <span className="pending-label">Searching the docs…</span>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Composer */}
      <div className="composer">
        <div className="composer-box">
          <span className="composer-prompt">›</span>
          <input
            ref={composerRef}
            type="text"
            value={chatInput}
            onChange={onChatInput}
            onKeyDown={onChatKey}
            placeholder="Ask about Claude, Codex, Gemini…"
            className="chat-input"
          />
          <button onClick={onSendClick} className="send-button" aria-label="Send">
            ↑
          </button>
        </div>
        <p className="composer-hint">
          CiteVyn answers from the official docs.{" "}
          {live ? "This answer was generated live, just now." : "This is a sample answer for the demo."}
        </p>
      </div>
    </main>
  );
}