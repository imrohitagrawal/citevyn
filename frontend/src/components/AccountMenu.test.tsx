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
};

vi.mock("../lib/api", () => ({
  // useAuth's mount effect calls bootstrapAuth() unconditionally, which
  // would otherwise overwrite the state seeded below the instant the
  // component mounts. Resolve consistently with it instead of stubbing
  // signed-in state and racing the real bootstrap call.
  getCurrentUser: vi.fn(() => Promise.resolve(SIGNED_IN_USER)),
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
});
