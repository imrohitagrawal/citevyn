/**
 * useLandingState — Encapsulates all CiteVyn landing page logic.
 *
 * Separates state/behavior from the template so the JSX stays readable
 * and the hook can be unit-tested independently.
 */

import React, { useCallback, useEffect, useRef, useReducer } from "react";
import { matchKB, matchCitevynMeta, KB, PLACEHOLDERS, type Source } from "../data/knowledgeBase";
import { askQuestion, createSession, isLiveMode } from "../lib/api";
import { citationsToSources } from "../lib/citations";
import { ApiClientError, type Suggestion } from "../lib/types";
import { useToast } from "./useToast";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

interface HeroState {
  key: string;
  text: string;
  streaming: boolean;
  showSources: boolean;
}

interface DemoState {
  key: string;
  text: string;
  streaming: boolean;
  done: boolean;
  refusal: boolean;
}

interface ChatMessage {
  /** Stable, monotonically-increasing id assigned at creation. Streamed
      updates target the message by this id (not by list position), so a
      still-streaming answer can never write into a later bubble even when the
      live path appends messages out of order after an awaited network call. */
  id: number;
  role: "user" | "bot";
  text: string;
  streaming?: boolean;
  sources?: Source[];
  refusal?: boolean;
  /** Nearest-doc suggestions on a graceful fallback (Phase 4a). */
  suggestions?: Suggestion[];
  /** A TRANSPORT failure (rate limit / server / network) — distinct from a content
   *  refusal (#120). Drives a rate-limit / error notice badge instead of the
   *  "NO SOURCE — REFUSED" badge, which must stay reserved for a genuine corpus miss. */
  errorKind?: "rate_limit" | "error";
}

interface AppState {
  heroInput: string;
  chatInput: string;
  phIndex: number;
  heroNudge: boolean;
  highlight: number;
  openFaq: number;
  hero: HeroState;
  demo: DemoState;
  messages: ChatMessage[];
  screen: "landing" | "chat";
  /** True between submitting a question and the first bot chunk landing.
      Drives the "thinking…" loader in ChatView. */
  pending: boolean;
  /** Monotonic counter bumped every time the user submits a NEW question. ChatView
      watches it to bring the just-asked question into view even when the reader had
      scrolled up — an explicit send must always be followed, unlike a passive stream
      append which respects the reader's scroll position. */
  sendTick: number;
}

type Action =
  | { type: "SET_HERO_INPUT"; value: string }
  | { type: "SET_CHAT_INPUT"; value: string }
  | { type: "ADVANCE_PLACEHOLDER" }
  | { type: "SET_HERO_NUDGE"; value: boolean }
  | { type: "SET_HIGHLIGHT"; index: number }
  | { type: "SET_OPEN_FAQ"; index: number }
  | { type: "SET_HERO"; hero: Partial<HeroState> }
  | { type: "SET_DEMO"; demo: Partial<DemoState> }
  | { type: "ADD_MESSAGE"; message: ChatMessage }
  | { type: "UPDATE_MESSAGE"; id: number; text: string }
  | {
      type: "FINISH_MESSAGE";
      id: number;
      sources: Source[];
      refusal?: boolean;
      suggestions?: Suggestion[];
    }
  | { type: "SET_SCREEN"; screen: "landing" | "chat" }
  | { type: "SET_PENDING"; value: boolean }
  | { type: "BUMP_SEND_TICK" };

const HERO_ORDER = ["claude-code", "gemini-key", "codex-flag"];

const initialState: AppState = {
  heroInput: "",
  chatInput: "",
  phIndex: 0,
  heroNudge: false,
  highlight: -1,
  openFaq: 0,
  hero: {
    key: "claude-code",
    text: "",
    streaming: true,
    showSources: false,
  },
  demo: {
    key: "claude-code",
    text: KB["claude-code"].a,
    streaming: false,
    done: true,
    refusal: false,
  },
  messages: [],
  screen: "landing",
  pending: false,
  sendTick: 0,
};

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "SET_HERO_INPUT":
      return { ...state, heroInput: action.value };
    case "SET_CHAT_INPUT":
      return { ...state, chatInput: action.value };
    case "ADVANCE_PLACEHOLDER":
      // Increment off the *current* state so the interval isn't stuck on a
      // value captured at mount time. Modulus derives from the single
      // PLACEHOLDERS source so adding a phrase needs no other edit.
      return { ...state, phIndex: (state.phIndex + 1) % PLACEHOLDERS.length };
    case "SET_HERO_NUDGE":
      return { ...state, heroNudge: action.value };
    case "SET_HIGHLIGHT":
      return { ...state, highlight: action.index };
    case "SET_OPEN_FAQ":
      return { ...state, openFaq: action.index };
    case "SET_HERO":
      return { ...state, hero: { ...state.hero, ...action.hero } };
    case "SET_DEMO":
      return { ...state, demo: { ...state.demo, ...action.demo } };
    case "ADD_MESSAGE":
      return { ...state, messages: [...state.messages, action.message] };
    case "UPDATE_MESSAGE":
      return {
        ...state,
        messages: state.messages.map((m) =>
          m.id === action.id ? { ...m, text: action.text } : m
        ),
      };
    case "FINISH_MESSAGE":
      return {
        ...state,
        messages: state.messages.map((m) =>
          m.id === action.id
            ? {
                ...m,
                streaming: false,
                sources: action.sources,
                refusal: action.refusal,
                suggestions: action.suggestions,
              }
            : m
        ),
      };
    case "SET_SCREEN":
      return { ...state, screen: action.screen };
    case "SET_PENDING":
      return { ...state, pending: action.value };
    case "BUMP_SEND_TICK":
      return { ...state, sendTick: state.sendTick + 1 };
    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// Timers
// ---------------------------------------------------------------------------

/**
 * A stoppable handle. Every timer (interval or timeout) is stored as one of
 * these so teardown is a uniform `t.stop()` — no juggling clearInterval vs
 * clearTimeout by hand.
 */
interface Timer {
  stop: () => void;
}

function timeout(fn: () => void, ms: number): Timer {
  const id = setTimeout(fn, ms);
  return { stop: () => clearTimeout(id) };
}

function interval(fn: () => void, ms: number): Timer {
  const id = setInterval(fn, ms);
  return { stop: () => clearInterval(id) };
}

/**
 * Smooth character-by-character streaming into `onChunk`; calls `onDone` once
 * the full string has been emitted. Returns a {@link Timer} that closes over
 * its own `clearInterval` so the caller can stop it without knowing it's an
 * interval.
 *
 * Implementation note: the previous word-by-word implementation burst whole
 * whitespace-separated tokens into a single frame, so a 14-char "**Features**"
 * marker, a 30-char paragraph, or a sequence of "  the  codex" whitespace
 * tokens all rendered in one ~16ms paint, then paused for ``delay`` ms — the
 * eye read this as a stuttery typewriter, not a smooth stream. Emitting a
 * few characters per tick (sized by ``delay`` so the wall-clock character
 * rate is steady) eliminates the burst-and-pause and keeps the rate even
 * across the whole text. ``delay=24`` + ``charsPerTick=2`` yields ~83 chars/sec,
 * which is the natural reading pace the eye accepts as "smooth".
 */
function streamText(
  full: string,
  onChunk: (chunk: string) => void,
  onDone?: () => void,
  delay = 24,
): Timer {
  const charsPerTick = Math.max(1, Math.round(delay / 12));
  let i = 0;
  let stopped = false;
  const id = setInterval(() => {
    // Defensive: an exception inside ``onChunk`` (e.g. a reducer invariant
    // violation) is swallowed by the browser's setInterval host and
    // bypasses our ``clearInterval`` below — the interval would keep
    // running forever, the pending indicator never clears, and the bot
    // bubble stays stuck with no text and no closing cursor. Wrap each
    // callback in try/catch so the interval is always cleared and the
    // failure is surfaced (console.error) instead of being silent.
    if (stopped) return;
    try {
      i = Math.min(full.length, i + charsPerTick);
      onChunk(full.slice(0, i));
      if (i >= full.length) {
        stopped = true;
        clearInterval(id);
        try {
          onDone?.();
        } catch (e) {
          console.error("[streamText] onDone threw:", e);
        }
      }
    } catch (e) {
      console.error("[streamText] onChunk threw; stopping stream:", e);
      stopped = true;
      clearInterval(id);
      try {
        onDone?.();
      } catch (e2) {
        console.error("[streamText] onDone threw after error:", e2);
      }
    }
  }, delay);
  return { stop: () => { stopped = true; clearInterval(id); } };
}

/** Smooth-scroll the section with `id` into view below the ~72px fixed header. */
function scrollToId(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  const y = el.getBoundingClientRect().top + window.pageYOffset - 72;
  window.scrollTo({ top: y, behavior: "smooth" });
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

interface TimerRefs {
  heroLoop: Timer | null;
  demoTimer: Timer | null;
  placeholderTimer: Timer | null;
  heroPause: Timer | null;
  nudgeTimeout: Timer | null;
  highlightTimeout: Timer | null;
}

export function useLandingState() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const timers = useRef<TimerRefs>({
    heroLoop: null,
    demoTimer: null,
    placeholderTimer: null,
    heroPause: null,
    nudgeTimeout: null,
    highlightTimeout: null,
  });

  // Chat answers stream concurrently: two live questions asked back-to-back
  // resolve close together and each drives its own bubble. A single timer
  // slot would let the second stream cancel the first (leaving the first
  // bubble empty with a stuck cursor), so every in-flight chat stream is held
  // here and self-removed on completion. Cleared en masse on unmount.
  const chatStreams = useRef<Set<Timer>>(new Set());

  const heroRef = useRef<HTMLInputElement>(null);

  // Toast surface for transport/rate-limit errors on the live path.
  const { toasts, addToast, removeToast } = useToast();

  // Whether the chat is wired to the real backend. Read once per render;
  // flips only when the ``VITE_API_LIVE`` build-time env changes.
  const live = isLiveMode();

  // Backend session, created lazily and reused across questions. The
  // promise ref de-dupes concurrent creation so two quick asks share
  // one session rather than opening two.
  const sessionIdRef = useRef<string | null>(null);
  const sessionPromiseRef = useRef<Promise<string> | null>(null);

  // Normalized questions whose last live attempt FAILED. The dedup guard
  // suppresses re-asking a question that was answered, but a transport
  // failure is not an answer — the user is told to retry, so a failed
  // question must be allowed back through.
  const failedQuestionsRef = useRef<Set<string>>(new Set());
  // Monotonic source of stable ChatMessage ids (see ChatMessage.id).
  const msgIdRef = useRef(0);
  const nextMessageId = useCallback(() => (msgIdRef.current += 1), []);

  // ---------------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------------

  useEffect(() => {
    playHeroLoop();

    timers.current.placeholderTimer = interval(
      () => dispatch({ type: "ADVANCE_PLACEHOLDER" }),
      3200,
    );

    // Keyboard shortcut: / focuses hero input
    const onKeyDown = (e: KeyboardEvent) => {
      if (
        e.key === "/" &&
        !/(INPUT|TEXTAREA)/.test(
          (document.activeElement as HTMLElement)?.tagName || ""
        )
      ) {
        e.preventDefault();
        document.getElementById("hero-input")?.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);

    return () => {
      // Clean up all timers — each knows how to stop itself.
      Object.values(timers.current).forEach((timer) => timer?.stop());
      chatStreams.current.forEach((t) => t.stop());
      chatStreams.current.clear();
      window.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  // ---------------------------------------------------------------------------
  // Hero auto-play
  // ---------------------------------------------------------------------------

  const playHeroLoop = useCallback(() => {
    let idx = 0;

    const play = () => {
      const key = HERO_ORDER[idx % HERO_ORDER.length];
      idx++;
      const entry = KB[key];

      dispatch({
        type: "SET_HERO",
        hero: { key, text: "", streaming: true, showSources: false },
      });

      timers.current.heroLoop = streamText(
        entry.a,
        (chunk) => dispatch({ type: "SET_HERO", hero: { text: chunk } }),
        () => {
          dispatch({ type: "SET_HERO", hero: { streaming: false, showSources: true } });
          timers.current.heroPause = timeout(play, 4600);
        },
      );
    };

    play();
  }, []);

  // ---------------------------------------------------------------------------
  // Demo
  // ---------------------------------------------------------------------------

  const selectDemo = useCallback(
    (key: string) => {
      const entry = KB[key];
      dispatch({
        type: "SET_DEMO",
        demo: {
          key,
          text: "",
          streaming: true,
          done: false,
          refusal: !!entry.refusal,
        },
      });

      timers.current.demoTimer = streamText(
        entry.a,
        (chunk) => dispatch({ type: "SET_DEMO", demo: { text: chunk } }),
        () =>
          dispatch({
            type: "SET_DEMO",
            demo: { streaming: false, done: true },
          }),
      );
    },
    [],
  );

  // ---------------------------------------------------------------------------
  // Hero ask
  // ---------------------------------------------------------------------------

  const onHeroInput = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) =>
      dispatch({ type: "SET_HERO_INPUT", value: e.target.value }),
    [],
  );

  const onFocusHero = useCallback(() => {
    heroRef.current?.focus();
  }, []);

  const onChatInput = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) =>
      dispatch({ type: "SET_CHAT_INPUT", value: e.target.value }),
    [],
  );

  // Create the backend session at most once. On failure the promise
  // cache is cleared so the next ask can retry cleanly rather than
  // being permanently wedged on a rejected promise.
  const ensureSession = useCallback(async (): Promise<string> => {
    if (sessionIdRef.current) return sessionIdRef.current;
    if (!sessionPromiseRef.current) {
      sessionPromiseRef.current = createSession()
        .then((res) => {
          sessionIdRef.current = res.session_id;
          return res.session_id;
        })
        .catch((err) => {
          sessionPromiseRef.current = null;
          throw err;
        });
    }
    return sessionPromiseRef.current;
  }, []);

  // Map an API/transport failure to a toast plus an inline refusal-styled
  // bot message. The inline copy is the durable surface — the toast
  // auto-dismisses, but a failed answer should stay legible in the
  // transcript. ``streamBot`` is a stable callback defined below.
  const handleApiError = useCallback(
    (err: unknown) => {
      const apiErr = err instanceof ApiClientError ? err : null;
      let title = "Something went wrong";
      let message = "The request failed. Please try again in a moment.";
      // A rate limit is a transient, user-recoverable condition, not a failure of
      // the service — so it gets a DISTINCT, less-alarming visual: the amber
      // "warning" toast (role="status", polite) rather than the red "error" alert
      // used for server/transport failures (Phase 4b: distinct 429 UI).
      let kind: "warning" | "error" = "error";
      // The inline bubble's badge: a TRANSPORT failure (rate limit / server / network)
      // must NOT wear the "NO SOURCE — REFUSED" content-refusal badge — that badge means
      // "the corpus had no answer", which is wrong here (#120). ``errorKind`` drives a
      // distinct rate-limit / connection-error notice instead.
      let errorKind: "rate_limit" | "error" = "error";
      if (apiErr?.isRateLimited()) {
        kind = "warning";
        errorKind = "rate_limit";
        title = "Rate limit reached";
        // Prefer the friendly copy over the raw "Request failed with status 429."
        message = "You've hit the demo rate limit — please wait a minute and try again.";
      } else if (apiErr?.isServerError()) {
        title = "Backend unavailable";
        message = "The answer service is temporarily unavailable. Please try again in a moment.";
      } else if (apiErr) {
        message = apiErr.message || message;
      }
      addToast({ kind, title, message });
      // ``refusal: false`` — this is a transport error, not a content refusal; the
      // ``errorKind`` badge is what the bubble shows.
      streamBot(message, { refusal: false, finalSources: [], errorKind });
    },
    // streamBot is a stable useCallback([]) declared below; intentionally
    // omitted to avoid a forward-reference in the dependency array.
    [addToast],
  );

  // Live answer path: ensure a session, ask the backend, and drive the
  // same streaming bubble the demo uses off the real answer + citations.
  const sendLive = useCallback(
    async (text: string) => {
      const norm = text.trim().toLowerCase();
      // Flip the loading indicator while we wait for the answer. We clear it
      // inside ``streamBot``'s first onChunk (see sendLive/setPending wiring)
      // — but to be safe against a backend that fails before the first chunk,
      // also clear it in the catch.
      dispatch({ type: "SET_PENDING", value: true });
      try {
        const sessionId = await ensureSession();
        const resp = await askQuestion(sessionId, text);
        failedQuestionsRef.current.delete(norm);
        streamBot(resp.answer, {
          refusal: resp.unsupported || resp.no_answer,
          finalSources: citationsToSources(resp.citations ?? []),
          // Graceful fallback (Phase 4a): surface nearest-doc suggestions the backend
          // offers on a no_answer/unsupported so the refusal isn't a dead end.
          finalSuggestions: resp.suggestions ?? [],
        });
      } catch (err) {
        // A 404 means the backend session expired or was evicted; drop the
        // cached id so the next attempt creates a fresh session instead of
        // wedging on the dead one forever.
        if (err instanceof ApiClientError && err.status === 404) {
          sessionIdRef.current = null;
          sessionPromiseRef.current = null;
        }
        // Remember the failure so the dedup guard lets the user retry it.
        failedQuestionsRef.current.add(norm);
        handleApiError(err);
        dispatch({ type: "SET_PENDING", value: false });
      }
    },
    [ensureSession, handleApiError],
  );

  // Route a question to the backend (live) or the canned KB (demo). Shared
  // by the first-ask and retry paths so both behave identically.
  const routeQuestion = useCallback(
    (text: string) => {
      if (live) {
        // Live: the backend now indexes an "About CiteVyn" source (#49), so
        // questions about CiteVyn itself (Pro/membership/coverage) flow through
        // retrieval + citation like any other question — no client short-circuit.
        void sendLive(text);
        return;
      }
      // Demo/offline: there is no backend, so CiteVyn-meta questions are
      // answered from the built-in copy (kept ONLY as the offline fallback),
      // and everything else falls back to the canned KB.
      const meta = matchCitevynMeta(text);
      if (meta) {
        // Meta answers are always affirmative product copy — never refusals.
        streamBot(meta.a, { refusal: false, finalSources: [] });
        return;
      }
      const hit = matchKB(text);
      streamBot(hit.a, {
        refusal: !!hit.refusal,
        finalSources: hit.sources || [],
      });
    },
    [live, sendLive],
  );

  const send = useCallback(
    (text: string) => {
      const norm = text.trim().toLowerCase();

      // Duplicate question guard
      const existing = state.messages.findIndex(
        (m) => m.role === "user" && m.text.trim().toLowerCase() === norm
      );
      if (existing !== -1) {
        // A prior attempt that FAILED is allowed to retry rather than just
        // flashing the original bubble — the guard only suppresses a
        // question that was actually answered.
        if (failedQuestionsRef.current.has(norm)) {
          failedQuestionsRef.current.delete(norm);
          // Re-show the user's question so the retry's response is not an orphaned
          // bot bubble with no visible question above it (#121).
          dispatch({
            type: "ADD_MESSAGE",
            message: { id: nextMessageId(), role: "user", text },
          });
          // An explicit (re)send must scroll to the new question even from a
          // scrolled-up position (a duplicate ANSWERED question takes the
          // flashExisting path below and keeps its own scroll behaviour).
          dispatch({ type: "BUMP_SEND_TICK" });
          routeQuestion(text);
          return;
        }
        flashExisting(existing);
        return;
      }

      dispatch({
        type: "ADD_MESSAGE",
        message: { id: nextMessageId(), role: "user", text },
      });
      dispatch({ type: "BUMP_SEND_TICK" });

      routeQuestion(text);
    },
    [state.messages, routeQuestion, nextMessageId],
  );

  const streamBot = useCallback(
    (
      text: string,
      extra: {
        refusal?: boolean;
        finalSources: Source[];
        finalSuggestions?: Suggestion[];
        errorKind?: "rate_limit" | "error";
      },
    ) => {
      // This answer's bubble gets its own stable id, and the stream targets that
      // id — never "the last message". So concurrent answers (e.g. two live
      // questions whose network calls resolve close together) each write only
      // into their own bubble: no text bleed, and each finalizes its own cursor.
      const id = nextMessageId();

      // Autoscroll is owned by ChatView: every dispatch below produces a new
      // `messages` array, and ChatView re-pins to the bottom after each add /
      // streamed chunk / finish ONLY while its stick-to-bottom latch is armed (a
      // reader who scrolled up keeps their position). An explicit send re-arms it.
      dispatch({
        type: "ADD_MESSAGE",
        message: { id, role: "bot", text: "", streaming: true, sources: [], ...extra },
      });

      // Each stream targets its own bubble by stable id, so concurrent
      // streams don't interfere and the previous one is NOT stopped — killing
      // it would leave its bubble empty with a stuck cursor. Track the handle
      // so unmount can stop any still-running stream; it self-removes on done.
      let handle: Timer;
      handle = streamText(
        text,
        (chunk) => {
          // First chunk arrived → drop the loading indicator so the typing
          // cursor takes over visually.
          dispatch({ type: "SET_PENDING", value: false });
          dispatch({ type: "UPDATE_MESSAGE", id, text: chunk });
        },
        () => {
          dispatch({
            type: "FINISH_MESSAGE",
            id,
            sources: extra.finalSources,
            refusal: extra.refusal,
            suggestions: extra.finalSuggestions,
          });
          chatStreams.current.delete(handle);
        },
      );
      chatStreams.current.add(handle);
    },
    [nextMessageId],
  );

  const flashExisting = useCallback(
    (index: number) => {
      // The duplicate is a user question at `index`. That is the anchor the
      // user wants re-confirmed, so:
      //   1. Scroll the user question into view (top of the chat list, with
      //      a small top inset for breathing room).
      //   2. Pulse ONLY the user bubble — the user said "I asked this
      //      before", they want to see THE QUESTION, not relive the
      //      answer. The answer below it is visible automatically once we
      //      scroll into view.
      //
      // The scroll math: the previous implementation used
      // ``(el.getBoundingClientRect().top - list.getBoundingClientRect().top)
      //   + list.scrollTop`` — that produces the element's top RELATIVE TO
      // the list's *unscrolled* coordinate system, which is the correct
      // ``scrollTop`` value to pin it at the list's visible top. But this
      // returns a NEGATIVE number if the element is ABOVE the list's
      // current viewport (i.e. the user has scrolled past it), which would
      // push ``scrollTop`` negative and silently fail to scroll. Clamp to
      // ``0`` so a question buried earlier in the chat rises to the top.
      dispatch({ type: "SET_HIGHLIGHT", index: -1 });
      setTimeout(() => {
        dispatch({ type: "SET_HIGHLIGHT", index });
        // Run the scroll on the next frame so the element exists in the
        // DOM (it always does, since it was rendered when the user first
        // asked) and so any layout from the highlight class is settled.
        // If the chat-list is missing (component unmounted) or has 0 height
        // (mid-re-mount), fall back to ``scrollIntoView`` on the bubble
        // itself — at minimum the user's browser will bring the bubble
        // into view, even if we can't pin it to the top of our list.
        requestAnimationFrame(() => {
          const el = document.getElementById(`cv-msg-${index}`);
          if (!el) return;
          const list = document.getElementById("chat-list");
          if (!list || list.clientHeight === 0) {
            el.scrollIntoView({ block: "start", behavior: "smooth" });
            return;
          }
          const desiredTop =
            el.getBoundingClientRect().top -
            list.getBoundingClientRect().top +
            list.scrollTop -
            12;
          list.scrollTo({
            top: Math.max(0, desiredTop),
            behavior: "smooth",
          });
        });
      }, 10);

      timers.current.highlightTimeout = timeout(
        () => dispatch({ type: "SET_HIGHLIGHT", index: -1 }),
        2000,
      );
    },
    [],
  );

  const enterChat = useCallback(
    (q: string | null) => {
      dispatch({ type: "SET_SCREEN", screen: "chat" });
      window.scrollTo({ top: 0 });
      if (q) {
        setTimeout(() => send(q), 60);
      }
    },
    [send],
  );

  const backToLanding = useCallback(() => {
    dispatch({ type: "SET_SCREEN", screen: "landing" });
    // A stale pending=true from a still-in-flight sendLive would survive
    // across the screen swap and re-appear as a phantom "Searching…"
    // bubble the next time the user enters chat. Clear it here so the
    // landing view (which doesn't render the bubble) doesn't leak state
    // into the next chat session.
    dispatch({ type: "SET_PENDING", value: false });
    // Clear the hero composer so returning to the landing page presents an empty
    // box — the prior question was already dispatched into chat and should not
    // linger for the user to delete before asking something new.
    dispatch({ type: "SET_HERO_INPUT", value: "" });
    window.scrollTo({ top: 0 });
  }, []);

  // Hero "Ask" is self-contained: on a valid question it navigates into chat
  // itself; on empty input it focuses the box and nudges. Defined *after*
  // `enterChat` so its useCallback dependency is in scope (no temporal-dead-zone).
  const askHero = useCallback(() => {
    const q = state.heroInput.trim();
    if (!q) {
      // Empty ask: focus the box, shake + amber border + inline warning
      heroRef.current?.focus();
      dispatch({ type: "SET_HERO_NUDGE", value: true });
      timers.current.nudgeTimeout = timeout(
        () => dispatch({ type: "SET_HERO_NUDGE", value: false }),
        3000,
      );
      return;
    }
    // Clear the hero box as the question is dispatched into chat (mirrors how
    // submitChat clears the chat composer), so a later "Back to landing" never
    // shows a stale question.
    dispatch({ type: "SET_HERO_INPUT", value: "" });
    enterChat(q);
  }, [state.heroInput, enterChat]);

  const onHeroKey = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") askHero();
    },
    [askHero],
  );

  // Submit the chat composer (button click or Enter). Trims, clears the input,
  // then routes the question through `send` (which handles the duplicate guard).
  const submitChat = useCallback(() => {
    const t = state.chatInput.trim();
    if (!t) return;
    dispatch({ type: "SET_CHAT_INPUT", value: "" });
    send(t);
  }, [state.chatInput, send]);

  const onChatKey = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") submitChat();
    },
    [submitChat],
  );

  // ---------------------------------------------------------------------------
  // Pricing / Pro flow
  // ---------------------------------------------------------------------------

  // "Get Pro" is just another question. Route it through `enterChat` → `send`
  // so the single dedup guard + streaming path handle it. In live mode it goes
  // to the backend (the About-CiteVyn source, #49); in demo mode it is answered
  // from built-in copy via `matchCitevynMeta` (which returns the KB "pro" entry).
  const getPro = useCallback(
    () => enterChat("What do I get with CiteVyn Pro?"),
    [enterChat],
  );

  // ---------------------------------------------------------------------------
  // FAQ
  // ---------------------------------------------------------------------------

  const toggleFaq = useCallback(
    (i: number) =>
      dispatch({ type: "SET_OPEN_FAQ", index: state.openFaq === i ? -1 : i }),
    [state.openFaq],
  );

  // ---------------------------------------------------------------------------
  // Navigation
  // ---------------------------------------------------------------------------

  const goSection = useCallback(
    (e: React.MouseEvent, id: string) => {
      e.preventDefault();
      // Nav links work from both views. From chat, return to landing first,
      // then scroll once React has remounted the sections.
      if (state.screen === "chat") {
        dispatch({ type: "SET_SCREEN", screen: "landing" });
        window.scrollTo({ top: 0 });
        setTimeout(() => scrollToId(id), 80);
      } else {
        scrollToId(id);
      }
    },
    [state.screen],
  );

  // ---------------------------------------------------------------------------
  // Derived data
  // ---------------------------------------------------------------------------

  const heroPlaceholder = PLACEHOLDERS[state.phIndex];

  const heroItem = KB[state.hero.key] || KB["claude-code"];
  const heroDots = HERO_ORDER.map((k) => ({
    active: k === state.hero.key,
    style: {
      width: k === state.hero.key ? "22px" : "7px",
      height: "7px",
      borderRadius: "999px",
      background: k === state.hero.key ? "var(--ink)" : "var(--border-2)",
      transition: "width .3s ease, background .3s ease",
    } as React.CSSProperties,
  }));

  const marqueeItems = [
    "claude-code",
    "gemini-key",
    "codex-flag",
    "claude-code-cost",
    "gemini-stream",
    "codex-install",
    "claude-models",
    "laptop",
  ].map((k) => ({
    q: KB[k].q,
    tag: KB[k].refusal ? "HONESTY" : KB[k].tag,
    select: () => enterChat(KB[k].q),
  }));

  const demoQuestions = ["claude-code", "codex-flag", "gemini-stream", "laptop"].map(
    (k) => {
      const active = k === state.demo.key;
      const entry = KB[k];
      return {
        key: k,
        q: entry.q,
        tag: entry.tag,
        active,
        select: () => selectDemo(k),
        btnStyle: {
          textAlign: "left" as const,
          cursor: "pointer" as const,
          borderRadius: "12px",
          padding: "13px 14px",
          border: `1px solid ${active ? "var(--ink)" : "var(--border)"}`,
          background: active ? "var(--surface)" : "transparent",
          color: "var(--ink)",
          boxShadow: active ? "0 2px 10px -6px rgba(0,0,0,0.3)" : "none",
        },
      };
    },
  );

  const heroChips = ["claude-code-cost", "codex-flag", "laptop"].map((k) => ({
    q: KB[k].q,
    select: () => enterChat(KB[k].q),
  }));

  const chatView = state.messages.map((m, i) => ({
    isUser: m.role === "user",
    isBot: m.role === "bot",
    domId: `cv-msg-${i}`,
    userStyle: {
      alignSelf: "flex-end",
      maxWidth: "78%",
      background: "var(--ink)",
      color: "var(--bg)",
      padding: "11px 16px",
      borderRadius: "16px 16px 4px 16px",
      fontSize: "15px",
      lineHeight: "1.5",
      transition: "box-shadow .3s ease",
      ...(state.highlight === i && {
        animation: "cv-pulse .55s ease-in-out 3",
        boxShadow: "0 0 0 3px var(--hl)",
      }),
    } as React.CSSProperties,
    text: m.text,
    streaming: !!m.streaming,
    refusal: !!m.refusal,
    errorKind: m.errorKind,
    hasSources: !m.streaming && (m.sources?.length ?? 0) > 0,
    sources: m.sources || [],
    // Nearest-doc suggestions on a graceful fallback (Phase 4a). Only shown once the
    // bubble has finished streaming and only when the backend offered any.
    docSuggestions: !m.streaming ? m.suggestions || [] : [],
  }));

  const chatSuggestions = ["claude-code", "codex-flag", "gemini-stream", "laptop"].map(
    (k) => ({
      q: KB[k].q,
      select: () => send(KB[k].q),
    }),
  );

  return {
    state,
    dispatch,
    enterChat,
    backToLanding,
    selectDemo,
    send,
    getPro,
    toggleFaq,
    goSection,
    heroItem,
    heroPlaceholder,
    heroDots,
    marqueeItems,
    demoQuestions,
    heroChips,
    chatView,
    chatSuggestions,
    openFaq: state.openFaq,
    heroRef,
    onHeroInput,
    onHeroKey,
    onAskHero: askHero,
    onChatInput,
    onChatKey,
    submitChat,
    onFocusHero,
    screen: state.screen,
    live,
    toasts,
    removeToast,
  };
}