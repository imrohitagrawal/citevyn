/**
 * Tests for the message bubble component.
 *
 * Message bubbles are the unit of rendering for every chat turn;
 * they handle four states (in-flight, grounded, refused, error).
 * These tests pin the visual contract so a CSS regression doesn't
 * slip through.
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { AssistantMessage, UserMessage } from "./MessageBubble";
import { groundedAnswerFixture, noAnswerFixture, unsupportedFixture } from "../test/fixtures";

describe("UserMessage", () => {
  it("renders the question text", () => {
    render(<UserMessage text="What is Claude 3.5 Sonnet?" />);
    expect(screen.getByText(/Claude 3\.5 Sonnet/)).toBeInTheDocument();
  });

  it("escapes user-supplied HTML to prevent XSS", () => {
    render(<UserMessage text="<img src=x onerror=alert(1)>" />);
    // The literal <img> must be rendered as text, never as an element.
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByText(/<img/)).toBeInTheDocument();
  });
});

describe("AssistantMessage", () => {
  it("renders the in-flight state with a typing indicator", () => {
    render(<AssistantMessage state={{ kind: "in-flight" }} />);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });

  it("renders the error state with a retry button when onRetry is provided", () => {
    const onRetry = vi.fn();
    render(
      <AssistantMessage
        state={{ kind: "error", message: "Network down", onRetry }}
      />,
    );
    const retry = screen.getByRole("button", { name: /retry/i });
    expect(retry).toBeInTheDocument();
  });

  it("renders the error state without a retry button when onRetry is omitted", () => {
    render(
      <AssistantMessage state={{ kind: "error", message: "boom" }} />,
    );
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });

  it("renders the grounded state with the answer and citation count", () => {
    render(
      <AssistantMessage
        state={{ kind: "grounded", response: groundedAnswerFixture }}
      />,
    );
    expect(screen.getByText(/October 2024/)).toBeInTheDocument();
    // The citation marker row carries an aria-label and visible label.
    expect(screen.getByLabelText(/Citations/)).toBeInTheDocument();
  });

  it("renders the no-answer state with the refusal copy", () => {
    render(
      <AssistantMessage
        state={{ kind: "refused", response: noAnswerFixture, reason: "no_answer" }}
      />,
    );
    expect(screen.getByText(/No grounded answer found/i)).toBeInTheDocument();
  });

  it("renders the unsupported state with the scope notice", () => {
    render(
      <AssistantMessage
        state={{ kind: "refused", response: unsupportedFixture, reason: "unsupported" }}
      />,
    );
    expect(screen.getByText(/Outside CiteVyn's scope/i)).toBeInTheDocument();
  });
});