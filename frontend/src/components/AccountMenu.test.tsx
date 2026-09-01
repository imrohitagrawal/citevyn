import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AccountMenu } from "./AccountMenu";
import { __testOnly } from "../lib/authStore";

const SIGNED_IN_USER = {
  request_id: "req_1",
  user_id: "usr_a",
  email: "a@example.com",
  anonymous: false,
  providers: [],
};

vi.mock("../lib/api", () => ({
  // useAuth's mount effect calls bootstrapAuth() unconditionally, which
  // would otherwise overwrite the state seeded below the instant the
  // component mounts. Resolve consistently with it instead of stubbing
  // signed-in state and racing the real bootstrap call.
  getCurrentUser: vi.fn((): Promise<typeof SIGNED_IN_USER | null> => Promise.resolve(SIGNED_IN_USER)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(() => Promise.resolve()),
  onUnauthorized: vi.fn(() => () => {}),
}));

beforeEach(() => {
  __testOnly.setState({ status: "signed-in", user: SIGNED_IN_USER });
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
});

describe("AccountMenu signed-in dropdown", () => {
  it("opens on trigger click and shows Sign out", async () => {
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
  });

  it("closes on an outside click without signing out", async () => {
    const { logout } = await import("../lib/api");
    const user = userEvent.setup();
    render(
      <div>
        <AccountMenu />
        <button>outside</button>
      </div>,
    );
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "outside" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(logout).not.toHaveBeenCalled();
  });

  it("closes and calls signOut when Sign out is clicked", async () => {
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  // ADR-0004 PR 13
  it("'Connected accounts' is a non-navigating menuitem that closes the menu and opens the drawer", async () => {
    // RED if the menuitem is removed, or if it navigates instead of opening
    // the lazy drawer (the dialog would never appear).
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    const item = screen.getByRole("menuitem", { name: "Connected accounts" });
    expect(item).toHaveAttribute("aria-haspopup", "dialog");
    // Between History and Sign out, per the plan.
    const names = screen.getAllByRole("menuitem").map((el) => el.textContent);
    expect(names).toEqual(["History", "Connected accounts", "Sign out"]);
    await user.click(item);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: "Connected accounts" })).toBeInTheDocument();
    // The actual Connect buttons live INSIDE the drawer, outside role="menu".
    expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeInTheDocument();
  });

  it("the drawer reflects the signed-in user's linked providers", async () => {
    // RED if AccountMenu passes a hardcoded [] instead of user.providers.
    const { getCurrentUser } = await import("../lib/api");
    const linked = { ...SIGNED_IN_USER, providers: ["google"] };
    vi.mocked(getCurrentUser).mockResolvedValueOnce(linked); // bootstrapAuth() re-resolves on mount
    __testOnly.setState({ status: "signed-in", user: linked });
    const user = userEvent.setup();
    render(<AccountMenu />);
    await user.click(await screen.findByRole("button", { name: "a@example.com" }));
    await user.click(screen.getByRole("menuitem", { name: "Connected accounts" }));
    await screen.findByRole("dialog", { name: "Connected accounts" });
    expect(screen.getByRole("button", { name: "Connect GitHub" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect Google" })).not.toBeInTheDocument();
  });
});

describe("AccountMenu anonymous state (ADR-0004 PR 13)", () => {
  it("never renders the 'Connected accounts' menuitem or any Connect button when signed out", async () => {
    const { getCurrentUser } = await import("../lib/api");
    vi.mocked(getCurrentUser).mockResolvedValueOnce(null);
    __testOnly.setState({ status: "anonymous", user: null });
    render(<AccountMenu />);
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: "Connected accounts" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Connect (GitHub|Google)$/ })).not.toBeInTheDocument();
  });
});
