/**
 * ADR-0005 Phase 6B-2: the account menu offers "API keys" only with the access
 * model on. Each test says which change turns it red.
 */
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AccountMenu } from "./AccountMenu";
import { __testOnly } from "../lib/authStore";
import { useClientConfig } from "../lib/clientConfig";

const USER = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: [],
  has_password: true,
  password_step_up: false,
};

vi.mock("../lib/api", () => ({
  getCurrentUser: vi.fn(() => Promise.resolve(USER)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(() => Promise.resolve()),
  onUnauthorized: vi.fn(() => () => {}),
}));
vi.mock("../lib/clientConfig", () => ({ useClientConfig: vi.fn() }));
vi.mock("./ApiKeysDrawer", () => ({ default: () => <div role="dialog" aria-label="API keys" /> }));
vi.mock("./SharedLinksDrawer", () => ({ default: () => <div role="dialog" aria-label="Shared links" /> }));
vi.mock("./UsageDrawer", () => ({ default: () => <div role="dialog" aria-label="Usage" /> }));

beforeEach(() => {
  __testOnly.setState({ status: "signed-in", user: USER });
  __testOnly.resetBootstrapped();
});
afterEach(() => cleanup());

describe("the API keys menu item", () => {
  it("opens the keys drawer with the access model on", async () => {
    // Turns red if: the item is missing, or does not open the drawer.
    vi.mocked(useClientConfig).mockReturnValue({
      access_model: true,
      bot_check: { kind: "pow" },
      allowance: { free_trial_answers: 25, pro_monthly_answers: 1000 },
    });
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "API keys" }));
    expect(await screen.findByRole("dialog", { name: "API keys" })).toBeInTheDocument();
  });

  it("partner: is not offered with the access model off", async () => {
    // Turns red if: the item shows while the model is off.
    vi.mocked(useClientConfig).mockReturnValue({ access_model: false, bot_check: null, allowance: null });
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "API keys" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Shared links" })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: "Usage" })).toBeNull();
  });

  it("offers Shared links with the access model on (ADR-0005 Phase 6C-2)", async () => {
    // Turns red if: the item is missing, or does not open its drawer.
    vi.mocked(useClientConfig).mockReturnValue({
      access_model: true,
      bot_check: { kind: "pow" },
      allowance: { free_trial_answers: 25, pro_monthly_answers: 1000 },
    });
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "Shared links" }));
    expect(await screen.findByRole("dialog", { name: "Shared links" })).toBeInTheDocument();
  });

  it("offers Usage with the access model on (ADR-0005 Phase 6D-2)", async () => {
    // Turns red if: the item is missing, or does not open its drawer.
    vi.mocked(useClientConfig).mockReturnValue({
      access_model: true,
      bot_check: { kind: "pow" },
      allowance: { free_trial_answers: 25, pro_monthly_answers: 1000 },
    });
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "Usage" }));
    expect(await screen.findByRole("dialog", { name: "Usage" })).toBeInTheDocument();
  });
});
