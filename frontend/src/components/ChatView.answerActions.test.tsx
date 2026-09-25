/**
 * ChatView mounts the lazily loaded feedback controls under a finished live
 * answer, and nowhere else (ADR-0005 §6).
 */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChatView } from "./ChatView";

type Props = Parameters<typeof ChatView>[0];
type Msg = Props["messages"][number];

const base = (over: Partial<Msg>): Msg => ({
  isUser: false,
  domId: "cv-msg-1",
  msgId: 1001,
  userStyle: {},
  text: "An answer.",
  ...over,
});

function renderWith(messages: Msg[]) {
  return render(
    <ChatView
      messages={messages}
      chatEmpty={false}
      chatSuggestions={[]}
      chatInput=""
      onChatInput={() => {}}
      onChatKey={() => {}}
      onSendClick={() => {}}
      onBackClick={() => {}}
      refusedInFlight={false}
    />,
  );
}

describe("feedback controls under an answer", () => {
  it("loads them under a finished live answer", async () => {
    // Turns red if: ChatView does not render the lazy controls for an answerRef.
    renderWith([base({ answerRef: { messageId: "m1", sessionId: "s1", question: "q" } })]);
    expect(await screen.findByRole("button", { name: "Helpful" })).toBeTruthy();
  });

  it("offers 'Request this source' on a refused live answer", async () => {
    // Turns red if: the refusal flag is not passed through to the controls.
    renderWith([
      base({ refusal: true, answerRef: { messageId: "m1", sessionId: "s1", question: "q" } }),
    ]);
    expect(await screen.findByRole("button", { name: "Request this source" })).toBeTruthy();
  });

  it("shows none under a demo answer, which has no backend id", async () => {
    // Turns red if: controls appear where a vote has nothing to attach to.
    renderWith([base({})]);
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole("button", { name: "Helpful" })).toBeNull();
    expect(screen.getByText("An answer.")).toBeTruthy(); // partner: the answer rendered
  });
});
