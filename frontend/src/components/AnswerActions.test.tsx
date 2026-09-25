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

  it("a thumbs down opens the report form, and the report carries its reason and comment", async () => {
    // Turns red if: "report wrong answer" loses the reason or the comment, or
    // sends a thumbs down with no way to say why.
    render(<AnswerActions {...REF} />);
    await click("Not helpful");
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
  it("is offered only on a refusal", () => {
    // Turns red if: every answer shows "Request this source", or a refusal hides it.
    const { unmount } = render(<AnswerActions {...REF} />);
    expect(screen.queryByRole("button", { name: "Request this source" })).toBeNull();
    unmount();
    render(<AnswerActions {...REF} refusal />);
    expect(screen.getByRole("button", { name: "Request this source" })).toBeTruthy();
  });

  it("logs the question, the refused answer and the reader's link", async () => {
    // Turns red if: the request drops the question, the message link or the URL.
    render(<AnswerActions {...REF} refusal />);
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
    render(<AnswerActions {...REF} refusal />);
    await click("Request this source");
    await click("Send request");
    expect(calls[0].body).toEqual({ question: "How do I set the model?", message_id: "m1" });
  });
});
