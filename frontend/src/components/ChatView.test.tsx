/**
 * ChatView — scroll ownership and composer handover (#302).
 *
 * The bug these guard: re-asking an already-answered question from a LANDING
 * section left the reader at the newest message instead of the answer they asked
 * to see. The scroll was performed imperatively from the landing hook, outside
 * the component that owns the scroll container, and raced its mount.
 */
import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ChatView } from "./ChatView";

type Props = Parameters<typeof ChatView>[0];
type Msg = Props["messages"][number];

function msg(i: number, isUser: boolean, text: string): Msg {
  // `msgId` is the STABLE id the hook assigns; `domId` is the list POSITION.
  // They are deliberately different values here so a test cannot pass by
  // confusing the two — the arrival announcement latches on msgId precisely
  // because a resumed transcript re-uses domId positions.
  return { isUser, domId: `cv-msg-${i}`, msgId: 1000 + i, userStyle: {}, text };
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
      // Clamp on WRITE and record the clamped value, exactly as a browser does:
      // `scrollTop = scrollHeight` lands at `scrollHeight - clientHeight`. `ops`
      // still records the value as ASSIGNED, so existing expectations of
      // "scrollTop=5000" (the intent) are unchanged. Without this the read-back
      // is unrealistic and the pin effect's own bookkeeping cannot be tested.
      const max = Math.max(0, this.scrollHeight - this.clientHeight);
      (this as unknown as { __st?: number }).__st = Math.min(Math.max(0, v), max);
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
      msgId: 2000 + i,
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

  it("shows no legend when the only marker matches no source", () => {
    // The legend explains chips. A gapped or dropped citation means [9] renders
    // as plain text, so a legend there would explain something not on screen.
    const { container } = renderChat({
      messages: [
        msg(0, true, "q"),
        {
          isUser: false,
          domId: "cv-msg-1",
          msgId: 3001,
          userStyle: {},
          text: "Grounded in [9].",
          hasSources: true,
          sources: [{ n: "1", title: "About CiteVyn", url: "/about" }],
        },
      ],
    });
    expect(container.querySelector(".citation-legend")).toBeNull();
    expect(container.querySelectorAll(".citation-chip")).toHaveLength(0);
    // Partner: the card IS rendered, so this is not passing on an empty render.
    expect(container.querySelectorAll(".source-card").length).toBe(1);
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
          msgId: 3001,
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

  // Found by the demo Playwright suite the FIRST time it ever ran in CI (#311),
  // which is the entire argument for having that job. On a slow runner it failed
  // with distanceFromBottom === 0: the reader's 40px scroll-up was silently undone.
  //
  // The disarm lives in the `scroll` EVENT listener, and the browser dispatches
  // that event asynchronously. If a streamed chunk commits in the window between
  // the scroll and its event, the passive `[messages]` effect still sees an armed
  // latch and pins to the bottom -- and then the scroll event arrives, reads "at
  // bottom" (because the pin just put it there) and RE-ARMS. The reader's scroll
  // is erased with no trace, which is exactly the #122 behaviour that latch exists
  // to provide.
  //
  // This test reproduces that ordering deterministically: move the list WITHOUT
  // dispatching a scroll event, then deliver a chunk. It fails on the pre-fix code
  // with `scrollTop=5000`.
  it("does not pin to the bottom when the reader has scrolled away but the scroll event has not fired yet", () => {
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat();
    const list = document.getElementById("chat-list") as unknown as { __st?: number };
    // Arm the latch the way arriving at the bottom does (5000 - 400 = 4600 -> distance 0).
    list.__st = 4600;
    document.getElementById("chat-list")!.dispatchEvent(new Event("scroll"));
    ops.length = 0;

    // The reader scrolls up 40px. The position changes; the EVENT has not run yet.
    list.__st = 4560;

    // A streamed chunk lands in that window.
    rerender(chat({ messages: [...MESSAGES] }));

    expect(ops.filter((o) => o.startsWith("scrollTop="))).toEqual([]);
  });

  it("still follows the stream while the reader IS at the bottom", () => {
    // Partner for the test above: proves the fix did not simply disable
    // stick-to-bottom, which would make that assertion pass vacuously.
    restoreLayout = installLayout(-500);
    const { rerender } = renderChat();
    const list = document.getElementById("chat-list") as unknown as { __st?: number };
    list.__st = 4600;
    document.getElementById("chat-list")!.dispatchEvent(new Event("scroll"));
    ops.length = 0;

    rerender(chat({ messages: [...MESSAGES] }));

    expect(ops).toContain("scrollTop=5000");
  });
});

/**
 * #62 — the composer while a live answer is in flight.
 *
 * The gate is deliberately ARIA-only on the send button, not the native
 * `disabled` attribute the rest of the app uses (`.cta:disabled`, the Enterprise
 * CTA). `disabled` is right for a control that is PERMANENTLY inert: it leaves
 * the tab order and nothing is lost. It is wrong for a control that flips busy
 * for the ~1s of a request, because the flip happens while the reader is very
 * likely focused on it (they just clicked Send) — the browser then drops focus
 * to `<body>` and the restore, a second later, puts it nowhere. `aria-disabled`
 * announces the same state, keeps focus and the tab order intact, and the click
 * handler is what actually refuses to act.
 */
describe("ChatView — composer while an answer is in flight (#62)", () => {
  it("marks the send button busy and announces why, without disabling the input", () => {
    renderChat({ pending: true });

    const send = screen.getByRole("button", { name: "Send" });
    expect(send).toHaveAttribute("aria-disabled", "true");
    // Not natively disabled: it must stay focusable and in the tab order.
    expect(send).not.toBeDisabled();
    // Type-ahead stays possible; disabling the input would also break the
    // focus-on-mount contract asserted above.
    const input = screen.getByPlaceholderText(/Ask about Claude/i);
    expect(input).not.toBeDisabled();
    // The reason is ANNOUNCED, not just drawn — from a region that was already
    // in the accessibility tree before the text arrived.
    expect(screen.getByRole("status")).toHaveTextContent(/Searching the docs/);
    // ...and the visible bubble is NOT a second live region announcing the
    // avatar's "CV" alongside it.
    expect(document.querySelector(".pending-msg")?.getAttribute("aria-live")).toBeNull();
  });

  it("leaves the send button live when nothing is in flight", () => {
    // Partner: proves the assertion above is not passing because the attribute
    // is simply always "true".
    renderChat({ pending: false });
    const send = screen.getByRole("button", { name: "Send" });
    expect(send.getAttribute("aria-disabled")).not.toBe("true");
    // The live region persists — that is the point of it — but it is empty, and
    // the visible loader is gone.
    expect(screen.getByRole("status")).toHaveTextContent("");
    expect(document.querySelector(".pending-bubble")).toBeNull();
  });

  it("shows the loader even when the transcript is still empty", () => {
    // It used to render only inside the non-empty branch, so resuming a
    // zero-message session mid-request left the send button aria-disabled with
    // nothing on screen saying why.
    renderChat({ pending: true, chatEmpty: true, messages: [] });
    expect(document.querySelector(".pending-bubble")).not.toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent(/Searching the docs/);
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("aria-disabled", "true");
  });

  // NOTE: there is deliberately no jsdom test that focus survives the flip.
  // One was written and DELETED: it passed with the whole #62 change absent,
  // and it passed with `disabled={pending}` — the exact design its own comment
  // said it rejected — because jsdom does not blur an element when it becomes
  // disabled. It asserted nothing this change could break. The property is real
  // and is asserted in a real browser instead, in the live-only Playwright test
  // in tests/behavior.spec.ts.

  it("routes the click to the hook in BOTH states — the refusal is the hook's, not the view's", () => {
    // This test asserted `onSendClick` was NOT called while pending, back when
    // the button was `onClick={pending ? undefined : onSendClick}`. That made
    // the click path unobservable (#356 gap 2): it never reached the hook, so
    // nothing could announce the refusal, and no prop this component has could
    // tell a refused click from no click at all.
    //
    // It also made the view a SECOND gate keyed on `pending`, which is a render
    // behind `inFlight.current` by construction (#62) — the less accurate of the
    // two. The gate is now the ref alone, in `submitChat`, which is where
    // `useLandingState.test.tsx` asserts no second request starts.
    //
    // What the reader gets is unchanged, and both halves are pinned here: the
    // control still announces itself unavailable, and no message is appended by
    // the click.
    const onSendClick = vi.fn();
    const { rerender } = renderChat({ pending: true, onSendClick });
    const send = screen.getByRole("button", { name: "Send" });

    expect(send).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(send);
    expect(onSendClick).toHaveBeenCalledTimes(1);

    // Partner: the wiring is not simply "always calls" because `pending` is
    // ignored — the attribute really does track it.
    rerender(chat({ pending: false, onSendClick }));
    const idle = screen.getByRole("button", { name: "Send" });
    expect(idle).toHaveAttribute("aria-disabled", "false");
    fireEvent.click(idle);
    expect(onSendClick).toHaveBeenCalledTimes(2);
  });
});

/**
 * #356 gap 2: a refused submit produced ZERO observable change.
 *
 * Measured on the shipping build with a MutationObserver over the whole body:
 * pressing Enter while an answer was in flight mutated nothing, on either the
 * Enter path or the click path. The gate was a bare `return` inside `submitChat`
 * and no signal reached the view, so `role="status"` had no text change to
 * announce.
 *
 * The signal is `refusalTick`, bumped at the `inFlight.current` ref itself. It
 * takes over the region the composer already renders rather than adding a second
 * one — the refusal copy is a superset of the in-flight sentence, `getByRole`
 * throws on two matching nodes, and there is no restore timer to get wrong: the
 * window closes on its own when `pending` drops.
 *
 * These tests drive the tick the way the hook drives it — a bump WHILE pending
 * is true — rather than setting the string directly. A test that renders the
 * refusal text and asserts it is present would pass with the whole feature
 * absent, which is the exact mistake the arrival announcement above had to be
 * rewritten to stop making.
 */
describe("ChatView announces a REFUSED submit (#356 gap 2)", () => {
  const REFUSED = /Not sent/;
  const INFLIGHT = /Searching the docs/;

  it("says nothing on mount, whatever the tick already is", () => {
    // A remount mid-flight (returning from the landing screen) must not
    // announce a refusal that happened before this component existed.
    renderChat({ pending: true, refusalTick: 7 });
    expect(screen.getByRole("status")).toHaveTextContent(INFLIGHT);
    expect(screen.getByRole("status").textContent).not.toMatch(REFUSED);
  });

  it("announces the refusal when the tick bumps while an answer is in flight", () => {
    const { rerender } = renderChat({ pending: true, refusalTick: 0 });
    expect(screen.getByRole("status")).toHaveTextContent(INFLIGHT);

    rerender(chat({ pending: true, refusalTick: 1 }));
    expect(screen.getByRole("status")).toHaveTextContent(REFUSED);
    // The copy has to carry the two facts the reader needs, or it is a beep:
    // WHY it was refused, and that their typing survived.
    expect(screen.getByRole("status")).toHaveTextContent(/still answering/);
    expect(screen.getByRole("status")).toHaveTextContent(/kept/);
  });

  // The two shapes the app really produces, in order — the same discipline the
  // arrival block above had to be rewritten to follow. `ADD_MESSAGE` appends a
  // streaming bubble with NO sources; `FINISH_MESSAGE` fills them in.
  const question = msg(4, true, "How much does it cost?");
  const streaming: Msg[] = [
    ...MESSAGES,
    question,
    { ...msg(5, false, ""), streaming: true, sources: [] },
  ];
  const done: Msg[] = [
    ...MESSAGES,
    question,
    {
      ...msg(5, false, "It is free during the preview."),
      streaming: false,
      sources: [{ n: "1", title: "Pricing", url: "https://example.com/pricing" }],
    },
  ];

  it("hands the region back to the arrival when the answer lands", () => {
    const { rerender } = renderChat({ pending: true, refusalTick: 0 });
    rerender(chat({ pending: true, refusalTick: 1 }));
    expect(screen.getByRole("status")).toHaveTextContent(REFUSED);

    // The window closes: `pending` drops in the same commit that appends the
    // streaming bubble, then the bubble finishes.
    rerender(chat({ pending: false, refusalTick: 1, messages: streaming }));
    expect(screen.getByRole("status")).toHaveTextContent("");

    rerender(chat({ pending: false, refusalTick: 1, messages: done }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 1 source cited.");
    expect(screen.getByRole("status").textContent).not.toMatch(REFUSED);
  });

  it("announces a refusal in a LATER in-flight window too", () => {
    // The partner for the stated limitation below. `role="status"` fires on a
    // text CHANGE, so the region must return to "" between windows or the
    // second window's first refusal is announced to nobody — the same defect
    // the arrival announcement was measured making across two answers.
    const { rerender } = renderChat({ pending: true, refusalTick: 0 });
    rerender(chat({ pending: true, refusalTick: 1 }));
    expect(screen.getByRole("status")).toHaveTextContent(REFUSED);

    rerender(chat({ pending: false, refusalTick: 1 }));
    expect(screen.getByRole("status")).toHaveTextContent("");

    rerender(chat({ pending: true, refusalTick: 1 }));
    expect(screen.getByRole("status")).toHaveTextContent(INFLIGHT);

    rerender(chat({ pending: true, refusalTick: 2 }));
    expect(screen.getByRole("status")).toHaveTextContent(REFUSED);
  });

  it("STATED LIMITATION: a second refusal in the SAME window does not re-announce", () => {
    // Pinned as a test rather than left as prose, so it is a decision and not a
    // surprise. The text is already the refusal, so a second bump changes
    // nothing and `role="status"` says nothing — correct, because the state has
    // not changed and the reader has already been told. Forcing a repeat means
    // clearing and re-setting across two commits, which is a timer and a flake.
    // If this ever needs to change, this test is where the decision is recorded.
    const { rerender } = renderChat({ pending: true, refusalTick: 0 });
    rerender(chat({ pending: true, refusalTick: 1 }));
    const before = screen.getByRole("status").textContent;
    expect(before).toMatch(REFUSED);
    rerender(chat({ pending: true, refusalTick: 2 }));
    expect(screen.getByRole("status").textContent).toBe(before);
  });
});

describe("ChatView announces an ARRIVING answer (#356)", () => {
  // The gap: when an answer lands, the pending region is removed, the answer
  // bubble is inserted and `aria-disabled` flips — none of it in a live region,
  // so a screen-reader user was never told. The error path already announces,
  // via ToastHost's role="alert".
  //
  // THESE TESTS DRIVE THE SHAPE THE APP ACTUALLY PRODUCES, which the first
  // version did not. `streamBot` appends the bot bubble with `sources: []` and
  // `streaming: true`; the real citations arrive later, at `FINISH_MESSAGE`.
  // The first version keyed on `pending` and handed in an already-settled
  // message list, so it asserted `"Answer ready. 2 sources cited."` — a string
  // the application could not emit, because at the instant `pending` dropped
  // the bubble was empty and sourceless. Three reviewers reproduced that
  // independently, one of them live in a browser. `streaming` going true→false
  // is the real edge: it is when the answer is readable AND when `sources` is
  // populated, on the live and demo paths alike.
  //
  // `streamed()` / `finished()` below are the two states in that order, so a
  // test that drives them cannot pass on a shape production never has.
  const question = msg(4, true, "How much does it cost?");
  /** What ADD_MESSAGE produces: empty text, streaming, NO sources yet. */
  const streamed = (over: Partial<Msg> = {}): Msg[] => [
    ...MESSAGES,
    question,
    { ...msg(5, false, ""), streaming: true, sources: [], ...over },
  ];
  /** What FINISH_MESSAGE produces: streaming off, sources populated. */
  const finished = (over: Partial<Msg> = {}): Msg[] => [
    ...MESSAGES,
    question,
    {
      ...msg(5, false, "It is free during the preview."),
      streaming: false,
      sources: [
        { n: "1", title: "Pricing", url: "https://example.com/pricing" },
        { n: "2", title: "Plans", url: "https://example.com/plans" },
      ],
      ...over,
    },
  ];

  it("says the answer is ready, with the source count, once the stream FINISHES", () => {
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    expect(screen.getByRole("status")).toHaveTextContent(/Searching the docs/);

    // The answer starts arriving and `pending` drops in the same commit. This
    // is the instant the old version announced, wrongly.
    rerender(chat({ pending: false, messages: streamed() }));
    expect(screen.getByRole("status")).toHaveTextContent("");

    rerender(chat({ pending: false, messages: finished() }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 2 sources cited.");
  });

  it("stays silent while the answer is still streaming", () => {
    // The #2 defect on its own: announcing at stream START told a reader the
    // answer was ready seconds before its last character rendered.
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    rerender(chat({ pending: false, messages: streamed() }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("announces on the DEMO path, where `pending` is never set at all", () => {
    // `markInFlight` is called only from `sendLive`, so in demo mode
    // `state.pending` is permanently 0. Keying on `pending` meant the region
    // said nothing to a demo/offline reader — and, because the REQUIRED
    // Playwright job runs in demo mode, that no browser test could see it.
    const { rerender } = renderChat({ pending: false, messages: MESSAGES });
    rerender(chat({ pending: false, messages: streamed() }));
    rerender(chat({ pending: false, messages: finished() }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 2 sources cited.");
  });

  it("says nothing when it mounts onto a transcript that was already finished", () => {
    // Returning to the chat screen, or resuming a past session, must not
    // announce an answer that arrived before the reader got here.
    renderChat({ pending: false, messages: finished() });
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("singularises one source rather than saying '1 sources'", () => {
    const one = finished({
      sources: [{ n: "1", title: "Pricing", url: "https://e.co/p" }],
    });
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    rerender(chat({ pending: false, messages: streamed() }));
    rerender(chat({ pending: false, messages: one }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 1 source cited.");
  });

  it("omits the source clause when the answer carries none", () => {
    const bare = finished({ sources: [] });
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    rerender(chat({ pending: false, messages: streamed() }));
    rerender(chat({ pending: false, messages: bare }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready.");
    expect(screen.getByRole("status").textContent).not.toMatch(/source/);
  });

  it("announces a REFUSAL as a refusal, not as an answer", () => {
    const refused = finished({ refusal: true, sources: [] });
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    rerender(chat({ pending: false, messages: streamed() }));
    rerender(chat({ pending: false, messages: refused }));
    expect(screen.getByRole("status")).toHaveTextContent(/No answer\./);
    expect(screen.getByRole("status").textContent).not.toMatch(/Answer ready/);
  });

  it("stays silent on a TRANSPORT failure, which ToastHost already announces as an alert", () => {
    const failed = finished({ errorKind: "error", sources: [] });
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    rerender(chat({ pending: false, messages: streamed({ errorKind: "error" }) }));
    rerender(chat({ pending: false, messages: failed }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("does not leave a stale 'answer ready' standing over a FAILED second request", () => {
    // The load-bearing case for clearing the text when a new request starts.
    // The error branch RETURNS without writing, so without the clear the region
    // would still read "Answer ready. 2 sources cited." over an error. The
    // first version of this test asserted the middle state while `pending` was
    // true — which the `pending ? … : settled` ternary supplies regardless of
    // what `settled` holds, so it could not fail. A reviewer deleted the clear
    // and the whole file stayed green.
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    rerender(chat({ pending: false, messages: streamed() }));
    rerender(chat({ pending: false, messages: finished() }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 2 sources cited.");

    const q2 = msg(6, true, "And after that?");
    const failing = [
      ...finished(),
      q2,
      { ...msg(7, false, ""), streaming: true, sources: [], errorKind: "error" as const },
    ];
    rerender(chat({ pending: false, messages: failing }));
    const settledFailure = [
      ...finished(),
      q2,
      {
        ...msg(7, false, "Something went wrong."),
        streaming: false,
        sources: [],
        errorKind: "error" as const,
      },
    ];
    rerender(chat({ pending: false, messages: settledFailure }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("does not announce when the newest message is the reader's own", () => {
    const { rerender } = renderChat({ pending: true, messages: MESSAGES });
    rerender(chat({ pending: false, messages: [...MESSAGES, msg(4, true, "Anyone there?")] }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  // ---- resumeSession: the transcript is REPLACED under a mounted ChatView ----
  //
  // `HistoryDrawer` lives on the chat screen, so `resumeSession` can fire at any
  // time — #62 gated the composer, not the drawer. A skeptic reproduced both of
  // these through the real hook against the boolean-latch version, which armed
  // on "the newest thing is unfinished" and then announced whatever the
  // replacement transcript happened to end with, WITH ITS SOURCE COUNT. Telling
  // an assistive-tech user an answer is ready for a question that has none is
  // precisely the harm this feature exists to prevent.
  //
  // The resumed messages carry ids the reader never saw stream, which is what
  // makes them distinguishable: `resumeSession` assigns fresh ids from the same
  // monotonic counter, so an id we are waiting on can never be re-used by a
  // replacement. Dropping the `!last` arm alone does NOT fix this — the second
  // test below resumes onto a tail that is a USER message and still fires.
  const resumed: Msg[] = [
    { ...msg(0, true, "an old question"), msgId: 7000 },
    {
      ...msg(1, false, "an old answer"),
      msgId: 7001,
      streaming: false,
      sources: [
        { n: "1", title: "Old", url: "https://e.co/1" },
        { n: "2", title: "Older", url: "https://e.co/2" },
      ],
    },
  ];

  it("does not announce a resumed transcript when the chat screen was EMPTY", () => {
    // The plain path: open the chat screen with no messages, then pick a
    // conversation from History. `messages` is `[]`, which the old code read as
    // "something is in progress" and armed on.
    const { rerender } = renderChat({ pending: false, messages: [], chatEmpty: true });
    expect(screen.getByRole("status")).toHaveTextContent("");
    rerender(chat({ pending: false, messages: resumed }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("does not announce a resumed transcript while the reader's own question is still open", () => {
    // The mid-flight path: a question is in flight (tail is the user's bubble,
    // no bot bubble yet) and the reader resumes a different conversation. The
    // old code announced the RESUMED conversation's answer and its source count
    // for a question that had not been answered at all.
    const { rerender } = renderChat({ pending: true, messages: [...MESSAGES, msg(4, true, "my question")] });
    expect(screen.getByRole("status")).toHaveTextContent(/Searching the docs/);
    rerender(chat({ pending: false, messages: resumed }));
    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("still announces an answer that arrives AFTER a resume, so the fix is not just silence", () => {
    // Partner for both tests above. Latching on identity must not turn the
    // feature off — a question asked in the resumed conversation still gets its
    // arrival announced.
    const { rerender } = renderChat({ pending: false, messages: resumed });
    const asking = [...resumed, { ...msg(2, true, "a new question"), msgId: 7002 }];
    rerender(chat({ pending: true, messages: asking }));
    const streaming = [
      ...asking,
      { ...msg(3, false, ""), msgId: 7003, streaming: true, sources: [] },
    ];
    rerender(chat({ pending: false, messages: streaming }));
    expect(screen.getByRole("status")).toHaveTextContent("");
    const done = [
      ...asking,
      {
        ...msg(3, false, "a new answer"),
        msgId: 7003,
        streaming: false,
        sources: [{ n: "1", title: "New", url: "https://e.co/n" }],
      },
    ];
    rerender(chat({ pending: false, messages: done }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 1 source cited.");
  });

  it("announces BOTH answers when two finish out of order", () => {
    // The tail-position defect: with A2 appended before A1, only
    // `messages[length - 1]` was ever read, so A2's arrival was announced twice
    // and A1's never.
    const q1 = { ...msg(0, true, "q1"), msgId: 8000 };
    const q2 = { ...msg(1, true, "q2"), msgId: 8001 };
    const a2streaming = { ...msg(2, false, ""), msgId: 8002, streaming: true, sources: [] };
    const a1streaming = { ...msg(3, false, ""), msgId: 8003, streaming: true, sources: [] };
    const { rerender } = renderChat({ messages: [q1, q2, a2streaming, a1streaming] });
    expect(screen.getByRole("status")).toHaveTextContent("");

    const a2done = {
      ...a2streaming,
      text: "second answer",
      streaming: false,
      sources: [{ n: "1", title: "Two", url: "https://e.co/2" }],
    };
    rerender(chat({ messages: [q1, q2, a2done, a1streaming] }));
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 1 source cited.");

    const a1done = {
      ...a1streaming,
      text: "first answer",
      streaming: false,
      sources: [
        { n: "1", title: "One", url: "https://e.co/1" },
        { n: "2", title: "Also", url: "https://e.co/1b" },
      ],
    };
    rerender(chat({ messages: [q1, q2, a2done, a1done] }));
    // The SECOND arrival is announced too, and with its OWN source count — not
    // the tail's, and not a repeat of the first.
    expect(screen.getByRole("status")).toHaveTextContent("Answer ready. 2 sources cited.");
  });

  it("announces the SECOND consecutive answer too, not just the first", () => {
    // `role="status"` announces on a text CHANGE. If the region never returns
    // to "" between two answers, the second one is announced to NOBODY because
    // the string is identical.
    //
    // This is not theoretical and it is not caught by any other test here: in
    // DEMO mode `send()` dispatches the user bubble and then `streamBot`'s bot
    // bubble synchronously, so React batches them and the tail is NEVER a user
    // message. A clear keyed only on `!last || last.isUser` therefore never
    // fires. Measured in a real browser at that commit, the region took the
    // values ["", "Answer ready. 1 source cited."] across two questions instead
    // of four. Deleting the clear survived all 40 other tests in this file.
    const seen: string[] = [];
    const record = () => {
      const t = screen.getByRole("status").textContent ?? "";
      if (seen[seen.length - 1] !== t) seen.push(t);
    };

    const one = (i: number) => [
      { ...msg(i, true, `q${i}`), msgId: 6000 + i },
      { ...msg(i + 1, false, ""), msgId: 6001 + i, streaming: true, sources: [] },
    ];
    const done = (i: number, src: string) => [
      { ...msg(i, true, `q${i}`), msgId: 6000 + i },
      {
        ...msg(i + 1, false, "an answer"),
        msgId: 6001 + i,
        streaming: false,
        sources: [{ n: "1", title: src, url: `https://e.co/${src}` }],
      },
    ];

    const { rerender } = renderChat({ messages: [] });
    record();
    // First question and answer. Both bubbles land in ONE commit, which is what
    // demo mode really does.
    rerender(chat({ messages: one(0) }));
    record();
    rerender(chat({ messages: done(0, "A") }));
    record();
    // Second question and answer, same shape.
    rerender(chat({ messages: [...done(0, "A"), ...one(10)] }));
    record();
    rerender(chat({ messages: [...done(0, "A"), ...done(10, "B")] }));
    record();

    // The region must have RETURNED to empty in between, or the second
    // announcement is a no-op string change that no screen reader will read.
    expect(seen).toEqual([
      "",
      "Answer ready. 1 source cited.",
      "",
      "Answer ready. 1 source cited.",
    ]);
  });

  it("gives the chat landmark an accessible name", () => {
    // Measured via Chromium's AX tree before this: `AX main: name=""`.
    renderChat();
    expect(screen.getByRole("main")).toHaveAccessibleName("Chat");
  });
});
