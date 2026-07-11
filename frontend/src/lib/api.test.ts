import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createSession, exactSearch, askQuestion, getHealth } from "./api";
import { ApiClientError } from "./types";

/**
 * Build a Response-like stub for the mocked ``fetch``. ``apiFetch``
 * only touches ``ok``, ``status``, and ``text()``, so we implement
 * exactly those.
 */
function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === null ? "" : JSON.stringify(body)),
  } as unknown as Response;
}

function lastCall(): { url: string; init: RequestInit } {
  const mock = fetch as unknown as ReturnType<typeof vi.fn>;
  const [url, init] = mock.mock.calls.at(-1) as [string, RequestInit];
  return { url, init };
}

function body(): Record<string, unknown> {
  return JSON.parse(lastCall().init.body as string);
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("exactSearch wire contract", () => {
  it("sends `limit` (the backend field), not `max_results`", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ request_id: "r", query: "--x", product_area: "codex", index_version: "v1", total: 0, hits: [] }),
    );

    await exactSearch({ term: "--model", product_area: "codex", max_results: 5 });

    const sent = body();
    expect(sent.limit).toBe(5);
    expect(sent).not.toHaveProperty("max_results");
    expect(sent.term).toBe("--model");
    expect(sent.product_area).toBe("codex");
  });

  it("defaults the limit to 10 when max_results is omitted", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ request_id: "r", query: "x", product_area: "codex", index_version: "v1", total: 0, hits: [] }),
    );

    await exactSearch({ term: "x", product_area: "codex" });

    expect(body().limit).toBe(10);
  });
});

describe("session + auth headers", () => {
  it("attaches the demo bearer token and posts the session body", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ request_id: "r", session_id: "s1", expires_at: "2026-07-11T00:00:00Z" }),
    );

    const res = await createSession();

    expect(res.session_id).toBe("s1");
    const { url, init } = lastCall();
    expect(url).toContain("/v1/sessions");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer local-demo-key");
    expect(body()).toMatchObject({ user_id: "demo_user", channel: "chat" });
  });

  it("posts the message + answer_style to the nested messages route", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ request_id: "r", message_id: "m1", answer: "hi", citations: [] }),
    );

    await askQuestion("sess-1", "How do permissions work?", "step_by_step");

    const { url } = lastCall();
    expect(url).toContain("/v1/sessions/sess-1/messages");
    expect(body()).toEqual({ message: "How do permissions work?", answer_style: "step_by_step" });
  });
});

describe("error envelope handling", () => {
  it("throws ApiClientError flagged as rate-limited on HTTP 429", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(
        { request_id: "r", status: "error", error: { code: "rate_limited", message: "Slow down." } },
        429,
      ),
    );

    await expect(getHealth()).rejects.toMatchObject({ name: "ApiClientError" });
    try {
      await getHealth();
    } catch (err) {
      const e = err as ApiClientError;
      expect(e.isRateLimited()).toBe(true);
      expect(e.message).toBe("Slow down.");
    }
  });

  it("flags 5xx as a server error", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ request_id: "r", status: "error", error: { code: "internal_error", message: "boom" } }, 503),
    );

    try {
      await getHealth();
      throw new Error("expected throw");
    } catch (err) {
      const e = err as ApiClientError;
      expect(e.isServerError()).toBe(true);
      expect(e.status).toBe(503);
    }
  });

  it("wraps a network failure as a status-0 ApiClientError", async () => {
    (fetch as ReturnType<typeof vi.fn>).mockRejectedValue(new TypeError("Failed to fetch"));

    try {
      await getHealth();
      throw new Error("expected throw");
    } catch (err) {
      const e = err as ApiClientError;
      expect(e).toBeInstanceOf(ApiClientError);
      expect(e.status).toBe(0);
    }
  });
});
