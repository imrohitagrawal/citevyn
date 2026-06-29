/**
 * Tests for the API client (``src/lib/api.ts``).
 *
 * These tests pin down the wire format the UI depends on. If the
 * backend's response shape changes, the type-level check at the
 * test boundary will fail and force an explicit update here.
 */
import { describe, expect, it, vi, beforeEach } from "vitest";

import {
  askQuestion,
  createSession,
  exactSearch,
  getHealth,
} from "./api";
import { ApiClientError } from "./types";
import {
  exactSearchFixture,
  groundedAnswerFixture,
  healthFixture,
  noAnswerFixture,
  sessionFixture,
} from "../test/fixtures";
import { mockFetch } from "../test/mockFetch";

describe("api client", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe("createSession", () => {
    it("POSTs to /v1/sessions and returns the session envelope", async () => {
      const fetchMock = mockFetch({
        "POST /v1/sessions": { body: sessionFixture },
      });

      const result = await createSession();

      expect(result).toEqual(sessionFixture);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("/v1/sessions");
      expect(init.method).toBe("POST");
      const body = JSON.parse(init.body as string);
      expect(body.user_id).toBe("demo_user");
      expect(body.channel).toBe("chat");
    });

    it("attaches the demo bearer token", async () => {
      const fetchMock = mockFetch({
        "POST /v1/sessions": { body: sessionFixture },
      });

      await createSession();

      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      const headers = new Headers(init.headers);
      expect(headers.get("Authorization")).toBe("Bearer local-demo-key");
      expect(headers.get("Content-Type")).toBe("application/json");
      expect(headers.get("Accept")).toBe("application/json");
    });

    it("passes through a custom user_id when provided", async () => {
      const fetchMock = mockFetch({
        "POST /v1/sessions": { body: sessionFixture },
      });

      await createSession({ user_id: "alice" });

      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(JSON.parse(init.body as string).user_id).toBe("alice");
    });
  });

  describe("askQuestion", () => {
    it("POSTs to /v1/sessions/:id/messages with the message body", async () => {
      const fetchMock = mockFetch({
        "POST /v1/sessions/abc-123/messages": { body: groundedAnswerFixture },
      });

      const result = await askQuestion("abc-123", "What is Claude 3.5 Sonnet?");

      expect(result).toEqual(groundedAnswerFixture);
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("/v1/sessions/abc-123/messages");
      expect(JSON.parse(init.body as string)).toEqual({
        message: "What is Claude 3.5 Sonnet?",
        answer_style: "short",
      });
    });

    it("defaults answer_style to 'short'", async () => {
      const fetchMock = mockFetch({
        "POST /v1/sessions/abc-123/messages": { body: groundedAnswerFixture },
      });

      await askQuestion("abc-123", "hi");

      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(JSON.parse(init.body as string).answer_style).toBe("short");
    });

    it("accepts 'step_by_step' answer style", async () => {
      const fetchMock = mockFetch({
        "POST /v1/sessions/abc-123/messages": { body: groundedAnswerFixture },
      });

      await askQuestion("abc-123", "how do I install?", "step_by_step");

      const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(JSON.parse(init.body as string).answer_style).toBe("step_by_step");
    });
  });

  describe("exactSearch", () => {
    it("POSTs the term and product_area to /v1/search/exact", async () => {
      const fetchMock = mockFetch({
        "POST /v1/search/exact": { body: exactSearchFixture },
      });

      const result = await exactSearch({ term: "max_tokens", product_area: "claude_api" });

      expect(result).toEqual(exactSearchFixture);
      const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe("/v1/search/exact");
      const body = JSON.parse(init.body as string);
      expect(body.term).toBe("max_tokens");
      expect(body.product_area).toBe("claude_api");
      expect(body.max_results).toBe(10);
    });
  });

  describe("getHealth", () => {
    it("GETs /health and returns the response", async () => {
      mockFetch({ "GET /health": { body: healthFixture } });

      const result = await getHealth();

      expect(result.status).toBe("healthy");
      expect(result.components).toEqual({ database: "healthy", redis: "healthy" });
    });
  });

  describe("error handling", () => {
    it("throws ApiClientError on a 429 with the envelope", async () => {
      mockFetch({
        "POST /v1/sessions/abc-123/messages": {
          status: 429,
          body: {
            status: "error",
            request_id: "req_rl",
            error: {
              code: "rate_limited",
              message: "Slow down — you can ask 30 questions per hour.",
            },
          },
        },
      });

      const promise = askQuestion("abc-123", "hi");
      await expect(promise).rejects.toBeInstanceOf(ApiClientError);
      await expect(promise).rejects.toMatchObject({
        status: 429,
        isRateLimited: expect.any(Function),
      });

      try {
        await askQuestion("abc-123", "hi");
      } catch (err) {
        expect((err as ApiClientError).isRateLimited()).toBe(true);
      }
    });

    it("throws ApiClientError on a 5xx", async () => {
      mockFetch({
        "POST /v1/sessions/abc-123/messages": {
          status: 500,
          body: { status: "error", error: { code: "internal", message: "boom" } },
        },
      });

      try {
        await askQuestion("abc-123", "hi");
        expect.fail("expected throw");
      } catch (err) {
        expect(err).toBeInstanceOf(ApiClientError);
        expect((err as ApiClientError).isServerError()).toBe(true);
      }
    });

    it("uses a fallback message when the error body is not JSON", async () => {
      mockFetch({
        "GET /health": {
          status: 502,
          body: "Bad Gateway",
        },
      });

      await expect(getHealth()).rejects.toThrow(/502/);
    });

    it("treats the no-answer flag as a successful response, not a throw", async () => {
      mockFetch({
        "POST /v1/sessions/abc-123/messages": { body: noAnswerFixture },
      });

      const result = await askQuestion("abc-123", "???");
      expect(result.no_answer).toBe(true);
      expect(result.answer).toMatch(/could not find/i);
    });
  });
});