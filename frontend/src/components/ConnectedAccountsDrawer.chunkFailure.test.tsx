import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { ConnectedAccountsDrawer } from "./ConnectedAccountsDrawer";
import { CHUNK_FAILED_MESSAGE } from "./LazyChunkBoundary";
import { __testOnly } from "../lib/authStore";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import postcss, { AtRule, Rule } from "postcss";

/**
 * #447: the drawer's "Set a password" button lazy-loads AuthModal. When that
 * chunk never arrives (a deploy replaced it under an open tab), the rejected
 * import used to unmount the whole React root.
 *
 * SEPARATE FILE ON PURPOSE: AuthModal is mocked to reject for the whole file,
 * and React caches a rejected lazy for the life of the module instance.
 *
 * RED if the LazyChunkBoundary around the drawer's AuthModal Suspense is
 * removed (the sibling and the drawer vanish with the root), or if its onError
 * stops closing the modal state / showing the message.
 *
 * The message is shown INSIDE the drawer, not as a toast: the toast stack sits
 * under the drawer's backdrop and behind its panel.
 */
vi.mock("./AuthModal", () => {
  throw new Error("Failed to fetch dynamically imported module: /assets/AuthModal-gone.js");
});

vi.mock("../lib/api", () => ({
  API_BASE_URL: "http://api.test",
  getCurrentUser: vi.fn(() => Promise.resolve(null)),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
  onUnauthorized: vi.fn(() => () => {}),
}));

let errorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  __testOnly.setState(__testOnly.initialState);
  __testOnly.resetBootstrapped();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ConnectedAccountsDrawer: the password dialog's chunk fails to load (#447)", () => {
  it("keeps the page and the drawer, leaves the button usable, and tells the reader", async () => {
    const onClose = vi.fn();
    const triggerRef = createRef<HTMLElement>();
    const user = userEvent.setup();
    render(
      <div>
        <p>the transcript so far</p>
        <ConnectedAccountsDrawer
          triggerRef={triggerRef}
          onClose={onClose}
          user={{ providers: [], has_password: false }}
        />
      </div>,
    );

    await user.click(screen.getByRole("button", { name: "Set a password" }));
    await waitFor(() => expect(errorSpy).toHaveBeenCalled());
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    // The partner to "no dialog": the reader is told, in the drawer.
    const firstAlert = await screen.findByRole("alert");
    expect(firstAlert).toHaveTextContent(CHUNK_FAILED_MESSAGE);
    expect(screen.queryByLabelText("New password")).not.toBeInTheDocument();
    // The drawer itself is untouched: only the dialog it tried to open is lost.
    expect(screen.getByRole("dialog", { name: "Sign-in methods" })).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(
      errorSpy.mock.calls.some((c: unknown[]) => String(c[0]).includes("[auth-modal]")),
    ).toBe(true);
    // Focus stays on the button that was clicked, inside the drawer: nothing
    // mounted, so nothing moved it.
    expect(screen.getByRole("button", { name: "Set a password" })).toHaveFocus();

    await user.click(screen.getByRole("button", { name: "Set a password" }));
    // Contained again: the second rejection is logged by the boundary again.
    await waitFor(() =>
      expect(
        errorSpy.mock.calls.filter((c: unknown[]) => String(c[0]).includes("[auth-modal]")).length,
      ).toBeGreaterThanOrEqual(2),
    );
    // A NEW alert node, not the same one left in place: a screen reader
    // announces role="alert" when it is inserted, so an unchanged node would
    // leave the second failure silent. RED if the alert stops being keyed on
    // the failure count.
    await waitFor(() => expect(screen.getByRole("alert")).not.toBe(firstAlert));
    expect(firstAlert).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(CHUNK_FAILED_MESSAGE);
    expect(screen.getByText("the transcript so far")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Sign-in methods" })).toBeInTheDocument();
  });
});

/**
 * The failure message is 13px text on the drawer panel, so it needs 4.5:1 in
 * BOTH themes (the floor #403 set). contrast-floor.spec.ts does not reach this
 * signed-in surface, so this resolves what the component EMITS - the alert's
 * inline `color` and the panel's inline `background` - against the real
 * tokens.css, for light and for both dark blocks.
 *
 * RED if the alert's colour goes back to a literal that fails one theme, e.g.
 * `#dc2626` (3.44:1 on the dark --surface #1e1e21).
 *
 * WHAT THIS CANNOT SEE: the rendered page. It assumes the drawer (portaled to
 * <body>) takes the `:root` tokens, and it reads inline styles only.
 */
const TOKENS_CSS = join(dirname(fileURLToPath(import.meta.url)), "..", "styles", "tokens.css");

function themeTokens(): Record<string, Record<string, string>> {
  const root = postcss.parse(readFileSync(TOKENS_CSS, "utf8"), { from: TOKENS_CSS });
  const light: Record<string, string> = {};
  const toggle: Record<string, string> = {};
  const os: Record<string, string> = {};
  const norm = (s: string) => s.replace(/\s+/g, " ").trim();
  root.walkRules((rule: Rule) => {
    const sel = norm(rule.selector);
    // The light block is a selector LIST (`:root, [data-theme="light"]`), so
    // match `:root` as one member of it, not as the whole selector.
    const members = sel.split(",").map((m) => m.trim());
    const parent = rule.parent;
    const inDarkMedia =
      parent?.type === "atrule" &&
      (parent as AtRule).name === "media" &&
      /prefers-color-scheme\s*:\s*dark/.test((parent as AtRule).params);
    const target =
      !inDarkMedia && members.includes(":root")
        ? light
        : !inDarkMedia && sel === '[data-theme="dark"]'
          ? toggle
          : inDarkMedia && sel.startsWith(":root")
            ? os
            : null;
    if (!target) return;
    rule.walkDecls((d) => {
      if (d.prop.startsWith("--")) target[d.prop] = d.value.trim();
    });
  });
  return {
    light,
    "dark (toggle)": { ...light, ...toggle },
    "dark (OS preference)": { ...light, ...os },
  };
}

function inlineDecl(el: Element, prop: string): string {
  const style = el.getAttribute("style") ?? "";
  const m = new RegExp(`(?:^|;)\\s*${prop}\\s*:\\s*([^;]+)`).exec(style);
  if (!m) throw new Error(`no inline ${prop} on element: ${style}`);
  return m[1].trim();
}

/** Resolve a `var(--x, fallback)` chain, then read hex or opaque rgb() into [r, g, b]. */
function resolveColor(value: string, tokens: Record<string, string>): number[] {
  const v = /^var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)$/.exec(value);
  const out = v ? (tokens[v[1]] ?? v[2]?.trim() ?? "") : value;
  if (out.startsWith("var(")) return resolveColor(out, tokens);
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(out);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join("") : hex[1];
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  }
  // jsdom rewrites an inline hex literal to rgb(); an alpha channel is refused.
  const rgb = /^rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)$/.exec(out);
  if (rgb) return rgb.slice(1, 4).map(Number);
  throw new Error(`cannot resolve ${value} to an opaque colour (got ${out})`);
}

function luminance(rgb: number[]): number {
  const [r, g, b] = rgb.map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: number[], b: number[]): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe("ConnectedAccountsDrawer: the chunk-failure message is readable in both themes (#447)", () => {
  it("clears 4.5:1 against the drawer panel in light and in both dark blocks", async () => {
    const user = userEvent.setup();
    render(
      <ConnectedAccountsDrawer
        triggerRef={createRef<HTMLElement>()}
        onClose={vi.fn()}
        user={{ providers: [], has_password: false }}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Set a password" }));
    const alert = await screen.findByRole("alert");
    const panel = screen.getByRole("dialog", { name: "Sign-in methods" });
    const fg = inlineDecl(alert, "color");
    const bg = inlineDecl(panel, "background");

    const themes = themeTokens();
    // Partner: the light tokens came from tokens.css itself, not from the
    // component's fallback literals, or "passes in light" measures only those.
    expect(themes.light["--surface"]).toBeDefined();
    expect(themes.light["--color-error"]).toBeDefined();
    // Partner: both dark blocks were found and really change the surface, or
    // "passes in dark" would be the light check run three times.
    expect(themes["dark (toggle)"]["--surface"]).not.toBe(themes.light["--surface"]);
    expect(themes["dark (OS preference)"]["--surface"]).not.toBe(themes.light["--surface"]);

    const results = Object.entries(themes).map(([theme, tokens]) => {
      const ratio =
        Math.round(contrast(resolveColor(fg, tokens), resolveColor(bg, tokens)) * 100) / 100;
      return { theme, ratio, passes: ratio >= 4.5 };
    });
    expect(results).toEqual(results.map((r) => ({ ...r, passes: true })));
  });
});
