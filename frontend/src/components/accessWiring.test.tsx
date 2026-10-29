/**
 * ADR-0005 Phase 5B-2: the pricing section and the usage meter appear ONLY with
 * the access model on. With it off, today's pricing section renders unchanged
 * and no meter mounts. Each test says which change turns it red.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { LandingStripBottom } from "./landing-strip";
import { ChatView } from "./ChatView";
import { useClientConfig } from "../lib/clientConfig";
import { getAuthSnapshot } from "../lib/authStore";

vi.mock("../lib/clientConfig", () => ({ useClientConfig: vi.fn() }));
const ANON = { status: "anonymous", user: null };
const SIGNED_IN = { status: "signed-in", user: { user_id: "usr_1", email: "a@example.com" } };
vi.mock("../lib/authStore", () => ({
  getAuthSnapshot: vi.fn(() => ANON),
  subscribeAuth: vi.fn(() => () => {}),
}));
vi.mock("./AccessPricing", () => ({ default: () => <p>access-pricing</p> }));
vi.mock("./UsageMeter", () => ({ default: () => <p>usage-meter</p> }));

const OFF = { access_model: false, bot_check: null, allowance: null };
const ON = {
  access_model: true,
  bot_check: { kind: "pow" },
  allowance: { free_trial_answers: 25, pro_monthly_answers: 1000 },
};

/** Let lazy chunks resolve before asserting ABSENCE: a lazy component renders
 * asynchronously, so an immediate "not there" passes whether or not it would
 * mount (a false pass the mutation run caught). */
async function settleLazy(): Promise<void> {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 50));
  });
}

afterEach(() => {
  cleanup();
  vi.mocked(getAuthSnapshot).mockReturnValue(ANON as ReturnType<typeof getAuthSnapshot>);
});

function strip() {
  return render(
    <LandingStripBottom onGetPro={vi.fn()} onOpenChat={vi.fn()} openFaq={-1} toggleFaq={vi.fn()} />,
  );
}

describe("the footer", () => {
  it.each([[OFF], [ON]])("links the published legal pages whatever the model says", async (config) => {
    // Turns red if: a footer link is dropped, points elsewhere, or only shows
    // with the model on (a payment provider's review needs them public).
    vi.mocked(useClientConfig).mockReturnValue(config);
    strip();
    await settleLazy();
    const nav = screen.getByRole("navigation", { name: "Legal" });
    const links = Array.from(nav.querySelectorAll("a")).map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([
      ["Terms", "/legal/terms"],
      ["Privacy", "/legal/privacy"],
      ["Refunds", "/legal/refunds"],
    ]);
  });
});

describe("the pricing section", () => {
  it("is today's section with the model off", async () => {
    // Turns red if: the new section renders while the model is off.
    vi.mocked(useClientConfig).mockReturnValue(OFF);
    strip();
    await settleLazy();
    expect(screen.getByText("$9")).toBeTruthy();
    expect(screen.queryByText("access-pricing")).toBeNull();
  });

  it("shows the decided Pro plan with the model off", async () => {
    // Turns red if: the card's price, yearly price or any Pro feature drifts
    // from the decided plan (ADR-0005), e.g. "Higher rate limits" comes back.
    vi.mocked(useClientConfig).mockReturnValue(OFF);
    strip();
    await settleLazy();
    const card = screen.getByText("Pro").closest(".pricing-card") as HTMLElement;
    expect(within(card).getByText("$9")).toBeTruthy();
    expect(within(card).getByText("/month or $91/year")).toBeTruthy();
    expect(within(card).getAllByRole("listitem").map((li) => li.textContent?.replace(/^✓\s*/, ""))).toEqual([
      "Everything in Demo",
      "1,000 answered questions a month",
      "MCP server and API keys for your coding tools",
      "Shareable answers",
      "Usage insights",
      "Alerts when the docs you watch change",
    ]);
  });

  it("is the new section with the model on", async () => {
    // Turns red if: the model being on does not swap the section.
    vi.mocked(useClientConfig).mockReturnValue(ON);
    strip();
    expect(await screen.findByText("access-pricing")).toBeTruthy();
    expect(screen.queryByText("$9")).toBeNull();
  });
});

function chat() {
  return render(
    <ChatView
      messages={[]}
      chatEmpty={true}
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

describe("the usage meter", () => {
  it("does not mount with the model off, even signed in", async () => {
    // Turns red if: the meter ignores the model setting.
    vi.mocked(useClientConfig).mockReturnValue(OFF);
    vi.mocked(getAuthSnapshot).mockReturnValue(SIGNED_IN as ReturnType<typeof getAuthSnapshot>);
    chat();
    await settleLazy();
    expect(screen.queryByText("usage-meter")).toBeNull();
  });

  it("does not mount for an anonymous visitor with the model on", async () => {
    // Turns red if: anonymous visitors get a meter (they have no allowance).
    vi.mocked(useClientConfig).mockReturnValue(ON);
    chat();
    await settleLazy();
    expect(screen.queryByText("usage-meter")).toBeNull();
  });

  it("mounts for a signed-in caller with the model on", async () => {
    // Turns red if: the meter never mounts.
    vi.mocked(useClientConfig).mockReturnValue(ON);
    vi.mocked(getAuthSnapshot).mockReturnValue(SIGNED_IN as ReturnType<typeof getAuthSnapshot>);
    chat();
    expect(await screen.findByText("usage-meter")).toBeTruthy();
  });
});
