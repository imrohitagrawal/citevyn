/**
 * useLandingState — Encapsulates all CiteVyn landing page logic.
 *
 * Separates state/behavior from the template so the JSX stays readable
 * and the hook can be unit-tested independently.
 */

import React, { useCallback, useEffect, useRef, useReducer } from "react";
import { matchKB, KB, type Source } from "../data/knowledgeBase";

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
  | { type: "SET_PH_INDEX"; index: number }
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
    case "SET_PH_INDEX":
      // Increment off the *current* state so the interval isn't stuck on a
      // value captured at mount time. Payload is ignored (kept for the action shape).
      return { ...state, phIndex: (state.phIndex + 1) % 5 };
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
// Streaming helper
// ---------------------------------------------------------------------------

function streamText(
  full: string,
  onChunk: (chunk: string) => void,
  onDone?: () => void,
  delay = 26,
): ReturnType<typeof setInterval> {
  // Split on whitespace (capturing group preserves whitespace tokens)
  const words = full.split(/(\s+)/);
  let i = 0;
  const timer = setInterval(() => {
    i++;
    if (i >= words.length) {
      clearInterval(timer);
      onChunk(full);
      onDone?.();
      return;
    }
    onChunk(words.slice(0, i).join(""));
  }, delay);
  return timer;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

interface TimerRefs {
  heroLoop: ReturnType<typeof setInterval> | null;
  demoTimer: ReturnType<typeof setInterval> | null;
  chatTimer: ReturnType<typeof setInterval> | null;
  placeholderTimer: ReturnType<typeof setInterval> | null;
  heroPause: ReturnType<typeof setTimeout> | null;
  nudgeTimeout: ReturnType<typeof setTimeout> | null;
  highlightTimeout: ReturnType<typeof setTimeout> | null;
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

  // ---------------------------------------------------------------------------
  // Lifecycle
  // ---------------------------------------------------------------------------

  useEffect(() => {
    playHeroLoop();

    timers.current.placeholderTimer = setInterval(
      () => dispatch({ type: "SET_PH_INDEX", index: 0 }),
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
      // Clean up all timers
      Object.values(timers.current).forEach((timer) => {
        if (timer !== null) clearTimeout(timer as any);
      });
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
          timers.current.heroPause = setTimeout(play, 4600);
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

  const onHeroKey = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") askHero();
    },
    [state.heroInput],
  );

  const onFocusHero = useCallback(() => {
    heroRef.current?.focus();
  }, []);

  const askHero = useCallback(() => {
    const q = state.heroInput.trim();
    if (!q) {
      // Empty ask: focus the box, shake + amber border + inline warning
      heroRef.current?.focus();
      dispatch({ type: "SET_HERO_NUDGE", value: true });
      timers.current.nudgeTimeout = setTimeout(
        () => dispatch({ type: "SET_HERO_NUDGE", value: false }),
        3000,
      );
      return;
    }
    return q;
  }, [state.heroInput]);

  const onChatInput = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) =>
      dispatch({ type: "SET_CHAT_INPUT", value: e.target.value }),
    [],
  );

  const send = useCallback(
    (text: string) => {
      const norm = text.trim().toLowerCase();

      // Duplicate question guard
      const existing = state.messages.findIndex(
        (m) => m.role === "user" && m.text.trim().toLowerCase() === norm
      );
      if (existing !== -1) {
        flashExisting(existing);
        return;
      }

      const hit = matchKB(text);
      dispatch({
        type: "ADD_MESSAGE",
        message: { role: "user", text },
      });
      streamBot(hit.a, {
        refusal: !!hit.refusal,
        finalSources: hit.sources || [],
      });
    },
    [state.messages],
  );

  const streamBot = useCallback(
    (text: string, extra: { refusal?: boolean; finalSources: Source[] }) => {
      dispatch({
        type: "ADD_MESSAGE",
        message: { role: "bot", text: "", streaming: true, sources: [], ...extra },
      });

      // Auto-scroll after adding message
      setTimeout(() => {
        const list = document.getElementById("chat-list");
        if (list) list.scrollTop = list.scrollHeight;
      }, 0);

      timers.current.chatTimer = streamText(
        text,
        (chunk) => {
          dispatch({ type: "UPDATE_LAST_MESSAGE", text: chunk });
          setTimeout(() => {
            const list = document.getElementById("chat-list");
            if (list) list.scrollTop = list.scrollHeight;
          }, 0);
        },
        () => {
          dispatch({
            type: "FINISH_LAST_MESSAGE",
            sources: extra.finalSources,
            refusal: extra.refusal,
          });
          setTimeout(() => {
            const list = document.getElementById("chat-list");
            if (list) list.scrollTop = list.scrollHeight;
          }, 0);
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

      timers.current.highlightTimeout = setTimeout(
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

  const getPro = useCallback(() => {
    const q = "What do I get with CiteVyn Pro?";
    dispatch({ type: "SET_SCREEN", screen: "chat" });
    window.scrollTo({ top: 0 });

    // Wait 80ms for chat view to render before sending
    setTimeout(() => {
      const norm = q.toLowerCase();
      const existing = state.messages.findIndex(
        (m) => m.role === "user" && m.text.trim().toLowerCase() === norm
      );
      if (existing !== -1) {
        flashExisting(existing);
        return;
      }
      dispatch({ type: "ADD_MESSAGE", message: { role: "user", text: q } });
      streamBot(
        "Pro isn't live yet — CiteVyn is an MVP demo, and everything here is free to try. Pro will add higher rate limits, exact lookups, saved history, and shareable answers. For now, ask me anything about Claude, Claude Code, Codex, or Gemini.",
        { refusal: false, finalSources: [] },
      );
    }, 80);
  }, [state.messages, streamBot, flashExisting]);

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
      // If on chat view, return to landing first (nav links work from both views)
      if (state.screen === "chat") {
        dispatch({ type: "SET_SCREEN", screen: "landing" });
        window.scrollTo({ top: 0 });
        // Give React time to remount the landing sections, then scroll
        setTimeout(() => {
          const el = document.getElementById(id);
          if (el) {
            const y = el.getBoundingClientRect().top + window.pageYOffset - 72;
            window.scrollTo({ top: y, behavior: "smooth" });
          }
        }, 80);
      } else {
        const el = document.getElementById(id);
        if (el) {
          const y = el.getBoundingClientRect().top + window.pageYOffset - 72;
          window.scrollTo({ top: y, behavior: "smooth" });
        }
      }
    },
    [state.screen],
  );

  // ---------------------------------------------------------------------------
  // Derived data
  // ---------------------------------------------------------------------------

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
    askHero,
    enterChat,
    backToLanding,
    selectDemo,
    send,
    getPro,
    toggleFaq,
    goSection,
    heroItem,
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
  };
}