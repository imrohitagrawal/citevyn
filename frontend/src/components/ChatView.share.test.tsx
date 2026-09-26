/**
 * A shared link belongs to ONE answer (ADR-0005 Phase 6C-2). Resuming another
 * chat reuses the same list positions (domId cv-msg-N), so the answer controls
 * must be keyed by the answer, or chat B's answer shows chat A's link.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatView } from "./ChatView";
import { shareAnswer } from "../lib/shares";

vi.mock("../lib/clientConfig", () => ({ useClientConfig: () => ({ access_model: true }) }));
vi.mock("../lib/shares", async (orig) => ({
  ...(await orig<typeof import("../lib/shares")>()),
  shareAnswer: vi.fn(),
}));

type Props = Parameters<typeof ChatView>[0];
type Msg = Props["messages"][number];

const answer = (messageId: string): Msg => ({
  isUser: false,
  domId: "cv-msg-1", // the same position in both chats
  msgId: 1001,
  userStyle: {},
  text: `Answer ${messageId}.`,
  answerRef: { messageId, sessionId: "s", question: "q", offerSourceRequest: false, shareable: true },
});

const view = (messages: Msg[]) => (
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
  />
);

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("the share link after resuming another chat", () => {
  it("is not shown under a different answer at the same position", async () => {
    // Turns red if: AnswerActions is keyed by list position instead of by answer.
    const id = "0123456789abcdef0123456789abcdef";
    vi.mocked(shareAnswer).mockResolvedValue({ share_id: id, url: `/s/${id}`, question: "q", created_at: "2026-09-26" });
    const { rerender } = render(view([answer("m-chat-a")]));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Share" }));
    expect(await screen.findByTestId("share-link")).toBeTruthy();
    rerender(view([answer("m-chat-b")]));
    expect(await screen.findByText("Answer m-chat-b.")).toBeTruthy();
    expect(screen.queryByTestId("share-link")).toBeNull();
  });
});
