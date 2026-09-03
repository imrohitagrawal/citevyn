/**
 * ChatView — scroll ownership and composer handover (#302).
 *
 * The bug these guard: re-asking an already-answered question from a LANDING
 * section left the reader at the newest message instead of the answer they asked
 * to see. The scroll was performed imperatively from the landing hook, outside
 * the component that owns the scroll container, and raced its mount.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
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
  return () => {
    Element.prototype.getBoundingClientRect = rect;
    if (clientHeight) Object.defineProperty(Element.prototype, "clientHeight", clientHeight);
  };
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
  // The list's own scroll offset is state, not layout, so it is set after mount.
  const list = document.getElementById("chat-list");
  if (list) list.scrollTop = SCROLL_TOP;
  return utils;
}

let restoreLayout: (() => void) | null = null;
const scrollTo = () => Element.prototype.scrollTo as unknown as ReturnType<typeof vi.fn>;

beforeEach(() => {
  Element.prototype.scrollTo = vi.fn() as unknown as Element["scrollTo"];
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  restoreLayout?.();
  restoreLayout = null;
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

  it("focuses the composer on mount so the chat is ready to type", () => {
    renderChat();
    expect(document.activeElement).toBe(screen.getByPlaceholderText(/Ask about Claude/i));
  });
});
