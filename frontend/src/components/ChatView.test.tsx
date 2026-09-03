/**
 * ChatView — scroll ownership and composer handover (#302).
 *
 * The bug these guard: re-asking an already-answered question from a LANDING
 * section left the reader at the newest message instead of the answer they asked
 * to see. The scroll was performed imperatively from the landing hook, outside
 * the component that owns the scroll container, and raced its mount.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { ChatView } from "./ChatView";

type Props = Parameters<typeof ChatView>[0];
type Msg = Props["messages"][number];

function msg(i: number, isUser: boolean, text: string): Msg {
  return { isUser, domId: `cv-msg-${i}`, userStyle: {}, text };
}

const MESSAGES: Msg[] = [
  msg(0, true, "What is Claude Code?"),
  msg(1, false, "Claude Code is an agentic coding tool."),
  msg(2, true, "How do I install the Codex CLI?"),
  msg(3, false, "Install it with npm."),
];

const LIST_TOP = 100;
const SCROLL_TOP = 900;

/**
 * jsdom has no layout engine, so every rect is zero and every element reports
 * ``clientHeight: 0`` — which would send the component down its zero-height
 * fallback and test nothing. Patch the PROTOTYPES before render so the geometry
 * is already in place when the mount-time layout effect measures.
 */
function installLayout(targetTop: number) {
  const rect = Element.prototype.getBoundingClientRect;
  const clientHeight = Object.getOwnPropertyDescriptor(Element.prototype, "clientHeight");
  Element.prototype.getBoundingClientRect = function (this: Element) {
    if (this.id === "chat-list") return { top: LIST_TOP, bottom: LIST_TOP + 400 } as DOMRect;
    if (this.id === "cv-msg-0") return { top: targetTop, bottom: targetTop + 40 } as DOMRect;
    return { top: 0, bottom: 0 } as DOMRect;
  };
  Object.defineProperty(Element.prototype, "clientHeight", {
    configurable: true,
    get(this: Element) {
      return this.id === "chat-list" ? 400 : 0;
    },
  });
  const scrollHeight = Object.getOwnPropertyDescriptor(Element.prototype, "scrollHeight");
  Object.defineProperty(Element.prototype, "scrollHeight", {
    configurable: true,
    get(this: Element) {
      return this.id === "chat-list" ? 5000 : 0;
    },
  });
  return () => {
    Element.prototype.getBoundingClientRect = rect;
    if (clientHeight) Object.defineProperty(Element.prototype, "clientHeight", clientHeight);
    if (scrollHeight) Object.defineProperty(Element.prototype, "scrollHeight", scrollHeight);
  };
}

function chat(props: Partial<Props> = {}) {
  return (
    <ChatView
      messages={MESSAGES}
      chatEmpty={false}
      chatSuggestions={[]}
      chatInput=""
      onChatInput={() => {}}
      onChatKey={() => {}}
      onSendClick={() => {}}
      onBackClick={() => {}}
      {...props}
    />
  );
}

function renderChat(props: Partial<Props> = {}) {
  const el = (
    <ChatView
      messages={MESSAGES}
      chatEmpty={false}
      chatSuggestions={[]}
      chatInput=""
      onChatInput={() => {}}
      onChatKey={() => {}}
      onSendClick={() => {}}
      onBackClick={() => {}}
      {...props}
    />
  );
  const utils = render(el);
  // The list's own scroll offset is state, not layout, so it is seeded after mount.
  // Written through the stub's backing field rather than the property, so the
  // harness's own seed never shows up in ``ops`` alongside the component's writes.
  const list = document.getElementById("chat-list") as unknown as { __st?: number } | null;
  if (list) list.__st = SCROLL_TOP;
  return utils;
}

let restoreLayout: (() => void) | null = null;
const scrollTo = () => Element.prototype.scrollTo as unknown as ReturnType<typeof vi.fn>;

/** Every scroll operation on the list, in order. ``scrollTo`` is the highlight jump;
 *  ``scrollTop=`` is a pin to the bottom. Ordering is the whole point: the bug was a
 *  jump immediately undone by a pin. */
let ops: string[] = [];
let restoreScrollTop: (() => void) | null = null;

/** jsdom ships no ``matchMedia``, so without this the reduced-motion branch is
 *  dead code in every unit test and every "smooth" assertion below would hold
 *  even if the OS preference were set. */
function stubReducedMotion(reduce: boolean) {
  (window as unknown as { matchMedia: unknown }).matchMedia = ((q: string) => ({
    matches: reduce && q.includes("prefers-reduced-motion"),
    media: q,
    addEventListener() {},
    removeEventListener() {},
  })) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
  ops = [];
  stubReducedMotion(false);
  Element.prototype.scrollTo = vi.fn(function (this: Element, o: ScrollToOptions) {
    ops.push(`scrollTo(${o.top})`);
  }) as unknown as Element["scrollTo"];
  Element.prototype.scrollIntoView = vi.fn(() => ops.push("scrollIntoView"));
  const prev = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
  Object.defineProperty(Element.prototype, "scrollTop", {
    configurable: true,
    get(this: Element) {
      return (this as unknown as { __st?: number }).__st ?? 0;
    },
    set(this: Element, v: number) {
      if (this.id === "chat-list") ops.push(`scrollTop=${v}`);
      (this as unknown as { __st?: number }).__st = v;
    },
  });
  restoreScrollTop = () => {
    if (prev) Object.defineProperty(Element.prototype, "scrollTop", prev);
  };
});

afterEach(() => {
  restoreLayout?.();
  restoreLayout = null;
  restoreScrollTop?.();
  restoreScrollTop = null;
});

describe("ChatView owns the duplicate-question scroll (#302)", () => {
  it("scrolls a highlight that is ALREADY pending on its very first render", () => {
    // This is the #302 case. A landing entry point switches screen and submits in
    // one gesture, so the highlight can be set before this component exists. The
    // old hook-side ``document.getElementById`` returned null here and gave up
    // silently. Nothing sets ``scrollTop`` before the effect runs on mount, so the
    // measured offset uses scrollTop 0: -500 - 100 + 0 - 12 → clamped to 0.
    restoreLayout = installLayout(-500);
    renderChat({ highlightedIndex: 0 });
    expect(scrollTo()).toHaveBeenCalledTimes(1);
    expect(scrollTo()).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
  });

  it("scrolls the highlighted bubble to the TOP of the list when the highlight arrives later", () => {
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat({ highlightedIndex: -1 });
    scrollTo().mockClear();
    rerender(
      <ChatView
        messages={MESSAGES}
        chatEmpty={false}
        chatSuggestions={[]}
        chatInput=""
        onChatInput={() => {}}
        onChatKey={() => {}}
        onSendClick={() => {}}
        onBackClick={() => {}}
        highlightedIndex={0}
      />,
    );
    // top = elTop(-500) - listTop(100) + scrollTop(900) - 12 = 288
    expect(scrollTo()).toHaveBeenCalledTimes(1);
    expect(scrollTo()).toHaveBeenCalledWith({ top: 288, behavior: "smooth" });
  });

  it("issues the scroll ONCE per highlight, not on every re-render", () => {
    // The landing hook rebuilds ``messages`` into a fresh array on every render and
    // the hero's demo animation re-renders continuously, so an effect that depended
    // on the array itself would restart the smooth scroll ~40x/second and it would
    // never settle. Depending on the highlighted bubble's id (a string) is what
    // keeps this to one scroll.
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat({ highlightedIndex: -1 });
    scrollTo().mockClear();
    const withHighlight = (messages: Msg[]) => (
      <ChatView
        messages={messages}
        chatEmpty={false}
        chatSuggestions={[]}
        chatInput=""
        onChatInput={() => {}}
        onChatKey={() => {}}
        onSendClick={() => {}}
        onBackClick={() => {}}
        highlightedIndex={0}
      />
    );
    rerender(withHighlight(MESSAGES));
    expect(scrollTo()).toHaveBeenCalledTimes(1);
    // Three renders with a NEW array identity but identical content — exactly what
    // the hero animation produces — must not re-issue the scroll.
    rerender(withHighlight([...MESSAGES]));
    rerender(withHighlight([...MESSAGES]));
    rerender(withHighlight([...MESSAGES]));
    expect(scrollTo()).toHaveBeenCalledTimes(1);
  });

  it("clamps at zero instead of scrolling to a negative offset", () => {
    // A negative ``top`` is silently ignored by the browser, so without the clamp
    // a question buried early in a long conversation never rises to the top.
    restoreLayout = installLayout(-5000);
    const { rerender } = renderChat({ highlightedIndex: -1 });
    scrollTo().mockClear();
    rerender(
      <ChatView
        messages={MESSAGES}
        chatEmpty={false}
        chatSuggestions={[]}
        chatInput=""
        onChatInput={() => {}}
        onChatKey={() => {}}
        onSendClick={() => {}}
        onBackClick={() => {}}
        highlightedIndex={0}
      />,
    );
    expect(scrollTo()).toHaveBeenCalledWith({ top: 0, behavior: "smooth" });
  });

  it("does not scroll while no bubble is highlighted", () => {
    restoreLayout = installLayout(-500);
    renderChat({ highlightedIndex: -1 });
    expect(scrollTo()).not.toHaveBeenCalled();
    // Partner assertion: prove the very same setup DOES scroll once a bubble is
    // highlighted, so this case cannot pass because the harness is inert.
    // ``cleanup()`` first: two live trees would put two ``#chat-list`` and two
    // ``#cv-msg-0`` nodes in the document, and the effect's ``getElementById``
    // would resolve the FIRST tree's bubble while its ref held the SECOND's list.
    cleanup();
    renderChat({ highlightedIndex: 0 });
    expect(scrollTo()).toHaveBeenCalled();
  });

  it("falls back to scrollIntoView when the list has no height yet", () => {
    // Mid-remount the container can measure zero; the reader should still be
    // taken to the bubble rather than left wherever they were.
    renderChat({ highlightedIndex: 0 });
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({
      block: "start",
      behavior: "smooth",
    });
    expect(scrollTo()).not.toHaveBeenCalled();
  });

  it("a highlight arriving with a NEW messages identity produces no pin to the bottom", () => {
    // Effect-PHASE ordering, which is the whole reason this is a layout effect.
    // Layout effects run before passive ones, so the highlight disarms the latch
    // before the passive ``[messages]`` pin reads it. As a plain ``useEffect``,
    // declaration order wins and the pin fires first — the fight this fix ends.
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat({ highlightedIndex: -1, sendTick: 2 });
    ops.length = 0;
    rerender(chat({ messages: [...MESSAGES], highlightedIndex: 0, sendTick: 2 }));
    expect(ops).toEqual(["scrollTo(288)"]);
  });

  it("does NOT pin to the bottom when it mounts with a highlight already pending", () => {
    // ``sendTick`` lives in the landing hook's reducer and outlives this component,
    // so every remount after an earlier send arrives with a non-zero tick. Guarding
    // that effect with ``sendTick === 0`` let it pin (and re-arm) right after the
    // highlight scroll, reinstating #302 on the remount path.
    restoreLayout = installLayout(-500);
    renderChat({ highlightedIndex: 0, sendTick: 3 });
    expect(ops.filter((o) => o.startsWith("scrollTop="))).toEqual([]);
    expect(ops).toContain("scrollTo(0)");
  });

  it("still pins to the bottom on mount when nothing is highlighted", () => {
    // Partner to the case above: the mount pin must survive for ordinary entry,
    // or "no pin" would be passing because the component stopped scrolling at all.
    restoreLayout = installLayout(-500);
    renderChat({ sendTick: 3 });
    expect(ops).toContain("scrollTop=5000");
  });

  it("still pins to the bottom when a NEW send bumps sendTick", () => {
    // Deliberately hostile setup: scroll AWAY first so the latch is disarmed, and
    // keep the ``messages`` array identity STABLE across the rerender. Without both,
    // the passive ``[messages]`` pin produces the very same ``scrollTop=5000`` and
    // the assertion holds with this effect deleted entirely.
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat({ sendTick: 3 });
    const list = document.getElementById("chat-list")!;
    list.scrollTop = 100;
    list.dispatchEvent(new Event("scroll"));
    ops.length = 0;
    rerender(chat({ sendTick: 4 })); // same MESSAGES reference
    expect(ops).toEqual(["scrollTop=5000"]);
  });

  it("while a highlight holds the list, reaching the bottom does NOT re-arm the latch", () => {
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat({ highlightedIndex: 0, sendTick: 2 });
    const list = document.getElementById("chat-list")!;
    list.scrollTop = 4600; // the true bottom: scrollHeight 5000 - clientHeight 400
    list.dispatchEvent(new Event("scroll"));
    ops.length = 0;
    rerender(chat({ messages: [...MESSAGES], highlightedIndex: 0, sendTick: 2 }));
    expect(ops).toEqual([]); // no pin => the latch stayed disarmed
  });

  it("but a reader who scrolls AWAY mid-highlight still disarms it", () => {
    // The hold is deliberately one-way. This is its partner: without it, the two
    // tests together would be satisfied by a hold that simply froze the latch.
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat({ highlightedIndex: 0, sendTick: 2 });
    // An explicit send is the only thing that re-arms while a highlight is held.
    rerender(chat({ messages: [...MESSAGES], highlightedIndex: 0, sendTick: 3 }));
    // Prove it: a fresh messages identity now pins. Without this the test would
    // also pass if the latch had simply never been re-armed.
    ops.length = 0;
    rerender(chat({ messages: [...MESSAGES], highlightedIndex: 0, sendTick: 3 }));
    expect(ops).toEqual(["scrollTop=5000"]);

    const list = document.getElementById("chat-list")!;
    list.scrollTop = 100; // scrolled well away from the bottom
    list.dispatchEvent(new Event("scroll"));
    ops.length = 0;
    rerender(chat({ messages: [...MESSAGES], highlightedIndex: 0, sendTick: 3 }));
    expect(ops).toEqual([]); // disarmed, despite the hold being active
  });

  it("jumps instantly instead of animating when the reader asked for less motion", () => {
    stubReducedMotion(true);
    restoreLayout = installLayout(-500);
    renderChat({ highlightedIndex: 0, sendTick: 3 });
    expect(scrollTo()).toHaveBeenCalledWith({ top: 0, behavior: "auto" });
  });

  it("releases the hold once the list has genuinely left the bottom", () => {
    // The hold only has to survive the first frames of a smooth scroll that has not
    // moved yet. Holding it any longer means a reader who scrolls back to the bottom
    // during the 2s highlight never re-arms stick-to-bottom — and because re-arming
    // is edge-triggered on a scroll event, that miss outlives the highlight itself.
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat({ highlightedIndex: 0, sendTick: 2 });
    const list = document.getElementById("chat-list")!;
    // The smooth scroll has now actually moved away from the bottom.
    list.scrollTop = 100;
    list.dispatchEvent(new Event("scroll"));
    // The reader scrolls back down to the true bottom themselves.
    list.scrollTop = 4600;
    list.dispatchEvent(new Event("scroll"));
    ops.length = 0;
    rerender(chat({ messages: [...MESSAGES], highlightedIndex: 0, sendTick: 2 }));
    expect(ops).toEqual(["scrollTop=5000"]); // following again
  });

  it("renders the reader's OWN text verbatim — no markdown, no chips", () => {
    // A question is the user's words, not the model's output. Formatting it would
    // rewrite what they typed, and a `[1]` in their question is not a citation.
    const { container } = renderChat({
      messages: [msg(0, true, "Why does **npm** print `[1]` here?")],
    });
    const bubble = container.querySelector(".message.user");
    expect(bubble?.textContent).toBe("Why does **npm** print `[1]` here?");
    expect(container.querySelector("strong")).toBeNull();
    expect(container.querySelector(".answer-code")).toBeNull();
    expect(container.querySelector(".citation-chip")).toBeNull();
  });

  it("shows the citation legend under the FIRST cited answer only", () => {
    const cited = (i: number, text: string): Msg => ({
      isUser: false,
      domId: `cv-msg-${i}`,
      userStyle: {},
      text,
      hasSources: true,
      sources: [{ n: "1", title: "About CiteVyn", url: "/about" }],
    });
    const { container } = renderChat({
      messages: [
        msg(0, true, "q1"),
        cited(1, "First cited answer [1]."),
        msg(2, true, "q2"),
        cited(3, "Second cited answer [1]."),
      ],
    });
    const legends = container.querySelectorAll(".citation-legend");
    expect(legends).toHaveLength(1);
    // ...and it is under the FIRST one, not just "somewhere".
    expect(container.querySelectorAll(".message.bot")[0].contains(legends[0])).toBe(true);
  });

  it("shows no legend when answers carry sources but no [n] markers (demo mode)", () => {
    // Demo answers have sources and zero markers, so no chips are rendered and
    // there is nothing for a legend to explain.
    const { container } = renderChat({
      messages: [
        msg(0, true, "q"),
        {
          isUser: false,
          domId: "cv-msg-1",
          userStyle: {},
          text: "A demo answer with no markers.",
          hasSources: true,
          sources: [{ n: "1", title: "About CiteVyn", url: "/about" }],
        },
      ],
    });
    expect(container.querySelector(".citation-legend")).toBeNull();
    // Partner: the cards ARE there, so this is not passing because nothing rendered.
    expect(container.querySelectorAll(".source-card").length).toBeGreaterThan(0);
  });

  it("focuses the composer on mount so the chat is ready to type", () => {
    renderChat();
    expect(document.activeElement).toBe(screen.getByPlaceholderText(/Ask about Claude/i));
  });
});
