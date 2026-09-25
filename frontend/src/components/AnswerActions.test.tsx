/**
 * Feedback under an answer (ADR-0005 §6 trust must-haves): thumbs up/down,
 * "report wrong answer" with a reason, and — on a refusal — "request this
 * source", which feeds the gap log. Lazily loaded; the chat never pays for it
 * up front.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import AnswerActions from "./AnswerActions";

type Call = { url: string; method: string; body: unknown };
let calls: Call[];
let fail = false;

beforeEach(() => {
  calls = [];
  fail = false;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(String(init.body)) : null });
      if (fail) {
        return new Response(JSON.stringify({ request_id: "r", status: "error", error: { code: "internal_error", message: "boom" } }), { status: 500 });
      }
      return new Response(JSON.stringify({ request_id: "r", status: "received" }), { status: 200 });
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

const REF = { sessionId: "s1", messageId: "m1", question: "How do I set the model?" };
const click = async (name: string | RegExp) => {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name }));
  });
};

describe("rating an answer", () => {
  it("a thumbs up is sent against THIS answer and shown as pressed", async () => {
    // Turns red if: the vote goes to the wrong route/answer, or the pressed state
    // is not exposed to assistive tech.
    render(<AnswerActions {...REF} />);
    await click("Helpful");
    expect(calls).toEqual([
      { url: expect.stringContaining("/v1/sessions/s1/messages/m1/feedback"), method: "PUT", body: { rating: "up" } },
    ]);
    expect(screen.getByRole("button", { name: "Helpful" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("'Report a problem' opens the report form, and the report carries its reason and comment", async () => {
    // Turns red if: "report wrong answer" loses the reason or the comment, or
    // sends a thumbs down with no way to say why.
    render(<AnswerActions {...REF} />);
    await click("Report a problem");
    expect(calls).toEqual([]); // nothing sent until the reader says why (or skips)
    fireEvent.click(screen.getByLabelText("Outdated"));
    fireEvent.change(screen.getByLabelText("What is wrong? (optional)"), {
      target: { value: "That flag was renamed." },
    });
    await click("Send report");
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("PUT");
    expect(calls[0].body).toEqual({ rating: "down", reason: "outdated", comment: "That flag was renamed." });
    expect(screen.getByTestId("answer-actions-status").textContent).toBe("Thanks, we logged your report.");
  });

  it("says so when the vote could not be saved", async () => {
    // Turns red if: a failed save is shown as a success.
    fail = true;
    render(<AnswerActions {...REF} />);
    await click("Helpful");
    expect(screen.getByTestId("answer-actions-status").textContent).toBe("Could not save that. Please try again.");
    expect(screen.getByRole("button", { name: "Helpful" }).getAttribute("aria-pressed")).toBe("false");
  });
});

describe("requesting a source on a refusal", () => {
  it("is offered only when asked to (an in-scope refusal)", () => {
    // Turns red if: every answer shows "Request this source", or it is hidden
    // when offered.
    const { unmount } = render(<AnswerActions {...REF} />);
    expect(screen.queryByRole("button", { name: "Request this source" })).toBeNull();
    unmount();
    render(<AnswerActions {...REF} offerSourceRequest />);
    expect(screen.getByRole("button", { name: "Request this source" })).toBeTruthy();
  });

  it("logs the question, the refused answer and the reader's link", async () => {
    // Turns red if: the request drops the question, the message link or the URL.
    render(<AnswerActions {...REF} offerSourceRequest />);
    await click("Request this source");
    fireEvent.change(screen.getByLabelText("Link to the page (optional)"), {
      target: { value: "https://docs.example.com/model" },
    });
    await click("Send request");
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual({
      url: expect.stringContaining("/v1/source-requests"),
      method: "POST",
      body: { question: "How do I set the model?", message_id: "m1", requested_url: "https://docs.example.com/model" },
    });
    expect(screen.getByTestId("answer-actions-status").textContent).toBe("Thanks, we logged your request.");
  });

  it("does not send an empty optional field", async () => {
    // The server refuses a non-http(s) URL; an empty box must mean "no URL".
    // Turns red if: empty strings are sent for the optional fields.
    render(<AnswerActions {...REF} offerSourceRequest />);
    await click("Request this source");
    await click("Send request");
    expect(calls[0].body).toEqual({ question: "How do I set the model?", message_id: "m1" });
  });
});

describe("review fixes (ADR-0005 Phase 3B review)", () => {
  it("the report comment and the source note are separate drafts", async () => {
    // Turns red if: one shared field carries a report comment into the gap log.
    render(<AnswerActions {...REF} offerSourceRequest />);
    await click("Report a problem");
    fireEvent.change(screen.getByLabelText("What is wrong? (optional)"), {
      target: { value: "private remark" },
    });
    await click("Request this source");
    expect((screen.getByLabelText("Anything else? (optional)") as HTMLTextAreaElement).value).toBe("");
    await click("Send request");
    expect(calls[0].body).toEqual({ question: "How do I set the model?", message_id: "m1" });
  });

  it("returns focus to the opening button after a report is sent", async () => {
    // Turns red if: the form unmounts with focus inside it (focus drops to <body>).
    render(<AnswerActions {...REF} />);
    await click("Report a problem");
    await click("Send report");
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Report a problem" }));
  });

  it("'Report a problem' is a disclosure, not a toggle; a sent report says so in text", async () => {
    // Turns red if: the disclosure also claims aria-pressed, or a report leaves no
    // visible trace.
    render(<AnswerActions {...REF} />);
    const btn = screen.getByRole("button", { name: "Report a problem" });
    expect(btn.hasAttribute("aria-pressed")).toBe(false);
    await click("Report a problem");
    await click("Send report");
    expect(screen.getByText("You reported this answer.")).toBeTruthy();
  });

  it("a 422 does not tell the reader to try again", async () => {
    // Turns red if: a rejection of THIS input is worded as a passing failure.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(
          JSON.stringify({ request_id: "r", status: "error", error: { code: "validation_error", message: "bad" } }),
          { status: 422 },
        ),
      ),
    );
    render(<AnswerActions {...REF} />);
    await click("Helpful");
    expect(screen.getByTestId("answer-actions-status").textContent).toBe(
      "That was not accepted. Please check what you entered.",
    );
  });

  it("a link without http(s) is refused here, announced, and kept", async () => {
    // Turns red if: the browser's own popup (or the server) is left to refuse it,
    // or the text is cleared.
    render(<AnswerActions {...REF} offerSourceRequest />);
    await click("Request this source");
    const link = screen.getByLabelText("Link to the page (optional)") as HTMLInputElement;
    fireEvent.change(link, { target: { value: "docs.example.com/page" } });
    await click("Send request");
    expect(calls).toEqual([]);
    expect(screen.getByTestId("answer-actions-status").textContent).toBe(
      "The link must start with http:// or https://.",
    );
    expect(link.value).toBe("docs.example.com/page");
  });
});

