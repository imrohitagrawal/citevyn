/**
 * useLandingState — Encapsulates all CiteVyn landing page logic.
 *
 * Separates state/behavior from the template so the JSX stays readable
 * and the hook can be unit-tested independently.
 */

import React, { useCallback, useEffect, useRef, useReducer } from "react";
import { matchKB, KB, PLACEHOLDERS, type Source } from "../data/knowledgeBase";
import { askQuestion, createSession, isLiveMode } from "../lib/api";
import { citationsToSources } from "../lib/citations";
import { ApiClientError } from "../lib/types";
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
  role: "user" | "bot";
  text: string;
  streaming?: boolean;
  sources?: Source[];
  refusal?: boolean;
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
  | { type: "UPDATE_LAST_MESSAGE"; text: string }
  | { type: "FINISH_LAST_MESSAGE"; sources: Source[]; refusal?: boolean }
  | { type: "SET_SCREEN"; screen: "landing" | "chat" };

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
    case "UPDATE_LAST_MESSAGE":
      return {
        ...state,
        messages: state.messages.map((m, i) =>
          i === state.messages.length - 1 ? { ...m, text: action.text } : m
        ),
      };
    case "FINISH_LAST_MESSAGE":
      return {
        ...state,
        messages: state.messages.map((m, i) =>
          i === state.messages.length - 1
            ? { ...m, streaming: false, sources: action.sources, refusal: action.refusal }
            : m
        ),
      };
    case "SET_SCREEN":
      return { ...state, screen: action.screen };
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
 * Word-by-word streaming into `onChunk`; calls `onDone` once the full string
 * has been emitted. Returns a {@link Timer} that closes over its own
 * `clearInterval` so the caller can stop it without knowing it's an interval.
 */
function streamText(
  full: string,
  onChunk: (chunk: string) => void,
  onDone?: () => void,
  delay = 26,
): Timer {
  // Split on whitespace (capturing group preserves whitespace tokens)
  const words = full.split(/(\s+)/);
  let i = 0;
  const id = setInterval(() => {
    i++;
    if (i >= words.length) {
      clearInterval(id);
      onChunk(full);
      onDone?.();
      return;
    }
    onChunk(words.slice(0, i).join(""));
  }, delay);
  return { stop: () => clearInterval(id) };
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
  chatTimer: Timer | null;
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
    chatTimer: null,
    placeholderTimer: null,
    heroPause: null,
    nudgeTimeout: null,
    highlightTimeout: null,
  });

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
      if (apiErr?.isRateLimited()) {
        title = "Rate limit reached";
        message =
          apiErr.message ||
          "You've hit the demo rate limit — wait a minute and try again.";
      } else if (apiErr?.isServerError()) {
        title = "Backend unavailable";
        message = apiErr.message || "The answer service is temporarily unavailable.";
      } else if (apiErr) {
        message = apiErr.message || message;
      }
      addToast({ kind: "error", title, message });
      streamBot(message, { refusal: true, finalSources: [] });
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
      try {
        const sessionId = await ensureSession();
        const resp = await askQuestion(sessionId, text);
        failedQuestionsRef.current.delete(norm);
        streamBot(resp.answer, {
          refusal: resp.unsupported || resp.no_answer,
          finalSources: citationsToSources(resp.citations ?? []),
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
      }
    },
    [ensureSession, handleApiError],
  );

  // Route a question to the backend (live) or the canned KB (demo). Shared
  // by the first-ask and retry paths so both behave identically.
  const routeQuestion = useCallback(
    (text: string) => {
      if (live) {
        void sendLive(text);
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
          routeQuestion(text);
          return;
        }
        flashExisting(existing);
        return;
      }

      dispatch({
        type: "ADD_MESSAGE",
        message: { role: "user", text },
      });

      routeQuestion(text);
    },
    [state.messages, routeQuestion],
  );

  const streamBot = useCallback(
    (text: string, extra: { refusal?: boolean; finalSources: Source[] }) => {
      // Autoscroll is owned by ChatView's useEffect([messages]): every dispatch
      // below produces a new `messages` array, so the view re-pins to the bottom
      // after each add / streamed chunk / finish. The hook no longer reaches into
      // #chat-list directly.
      dispatch({
        type: "ADD_MESSAGE",
        message: { role: "bot", text: "", streaming: true, sources: [], ...extra },
      });

      timers.current.chatTimer = streamText(
        text,
        (chunk) => dispatch({ type: "UPDATE_LAST_MESSAGE", text: chunk }),
        () => {
          dispatch({
            type: "FINISH_LAST_MESSAGE",
            sources: extra.finalSources,
            refusal: extra.refusal,
          });
        },
      );
    },
    [],
  );

  const flashExisting = useCallback(
    (index: number) => {
      // Reset highlight first to restart animation
      dispatch({ type: "SET_HIGHLIGHT", index: -1 });
      setTimeout(() => {
        dispatch({ type: "SET_HIGHLIGHT", index });
        setTimeout(() => {
          const el = document.getElementById(`cv-msg-${index}`);
          const list = document.getElementById("chat-list");
          if (el && list) {
            const top =
              el.getBoundingClientRect().top -
              list.getBoundingClientRect().top +
              list.scrollTop -
              12;
            list.scrollTo({ top, behavior: "smooth" });
          }
        }, 40);
      }, 10);

      timers.current.highlightTimeout = timeout(
        () => dispatch({ type: "SET_HIGHLIGHT", index: -1 }),
        2100,
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
  // so the single dedup guard + streaming path handle it (the canned answer
  // lives in the KB as the "pro" entry, matched by `matchKB`).
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
    hasSources: !m.streaming && (m.sources?.length ?? 0) > 0,
    sources: m.sources || [],
  }));

  const chatSuggestions = ["claude-code", "codex-flag", "gemini-stream", "laptop"].map(
    (k) => ({
      q: KB[k].q,
      select: () => send(KB[k].q),
    }),
  );

  const faqItems = [
    { q: "Which tools does CiteVyn cover?", a: "The MVP covers Claude (API), Claude Code, OpenAI Codex, and Google Gemini — using their official documentation only. ChatGPT and Cursor are on the roadmap, not in the MVP." },
    { q: "How do citations work?", a: "Every factual answer is generated only from retrieved documentation chunks, and each is attached to the exact source page it came from. If a claim isn't supported by a source, it isn't made." },
    { q: "What happens when it can't find an answer?", a: "CiteVyn refuses rather than guesses. If the docs don't support a reliable answer, or the question is outside the supported tools, it tells you so plainly instead of hallucinating." },
    { q: "Does CiteVyn hallucinate?", a: "It's designed not to. Answers are grounded in indexed official docs and gated by an evaluation suite targeting 95%+ citation correctness and faithfulness before release." },
    { q: "Can it answer questions about my private docs?", a: "Not in the MVP — it uses public official documentation only. Private-source ingestion, SSO, and tenant isolation are part of the Enterprise roadmap." },
    { q: "How fresh is the documentation?", a: "CiteVyn serves from the last known-good index, so a failed re-index never corrupts what's live. Scheduled source refresh is an Enterprise feature." },
  ].map((f, i) => ({
    q: f.q,
    a: f.a,
    open: state.openFaq === i,
    sign: state.openFaq === i ? "−" : "+",
    toggle: () => toggleFaq(i),
  }));

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
    faqItems,
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