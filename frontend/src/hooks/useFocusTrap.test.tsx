import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { useFocusTrap } from "./useFocusTrap";
import { isModalDialogOpen, __testOnly } from "../lib/dialogStack";

afterEach(cleanup);

/**
 * Direct tests for the hook (#331), covering the edges the two drawers cannot
 * reach because both always render a Close button:
 *   - a dialog with NO focusable control at all
 *   - WHICH element the pull-back branch chooses, not merely that focus stayed
 *     inside — a pull-back that always went forward survived the drawer tests,
 *     because landing on the wrong end is still "inside the dialog"
 */
function Harness({
  controls,
  onEscape,
  label = "Harness",
}: {
  controls: string[];
  onEscape?: () => void;
  label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(ref, { onEscape });
  return (
    <>
      <button type="button">outside-before</button>
      <div ref={ref} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1}>
        {controls.map((c) => (
          <button type="button" key={c}>
            {c}
          </button>
        ))}
      </div>
      <button type="button">outside-after</button>
    </>
  );
}

const dialog = () => screen.getByRole("dialog", { name: "Harness" });
const activeText = () => (document.activeElement as HTMLElement | null)?.textContent;

describe("useFocusTrap", () => {
  it("Shift+Tab from the dialog itself goes to the LAST control, not the first", async () => {
    // The natural backward destination. A pull-back that always chose `first`
    // kept focus inside the dialog and so passed the drawers' tests, while
    // reversing the reader's direction of travel.
    const user = userEvent.setup();
    render(<Harness controls={["one", "two", "three"]} />);
    dialog().focus();
    await user.tab({ shift: true });
    expect(activeText()).toBe("three");
  });

  it("Tab from the dialog itself goes to the FIRST control", async () => {
    const user = userEvent.setup();
    render(<Harness controls={["one", "two", "three"]} />);
    dialog().focus();
    await user.tab();
    expect(activeText()).toBe("one");
  });

  it("wraps forward off the last control back to the first", async () => {
    const user = userEvent.setup();
    render(<Harness controls={["one", "two"]} />);
    screen.getByText("two").focus();
    await user.tab();
    expect(activeText()).toBe("one");
  });

  it("wraps backward off the first control to the last", async () => {
    const user = userEvent.setup();
    render(<Harness controls={["one", "two"]} />);
    screen.getByText("one").focus();
    await user.tab({ shift: true });
    expect(activeText()).toBe("two");
  });

  // A dialog with nothing focusable must still not leak Tab to the page. The
  // drawers cannot exercise this — both always render a Close button.
  it("keeps focus in an EMPTY dialog rather than letting Tab escape", async () => {
    const user = userEvent.setup();
    render(<Harness controls={[]} />);
    dialog().focus();
    await user.tab();
    expect(dialog()).toHaveFocus();
    expect(activeText()).not.toBe("outside-after");
    await user.tab({ shift: true });
    expect(dialog()).toHaveFocus();
    expect(activeText()).not.toBe("outside-before");
  });

  it("pulls focus back in if it is already on a control outside the dialog", async () => {
    const user = userEvent.setup();
    render(<Harness controls={["one", "two"]} />);
    screen.getByText("outside-before").focus();
    await user.tab();
    expect(activeText()).toBe("one");
  });

  it("calls onEscape on Escape", async () => {
    const user = userEvent.setup();
    const onEscape = vi.fn();
    render(<Harness controls={["one"]} onEscape={onEscape} />);
    await user.keyboard("{Escape}");
    expect(onEscape).toHaveBeenCalledTimes(1);
  });

  it("leaves Escape alone when no handler is given", async () => {
    const user = userEvent.setup();
    render(<Harness controls={["one"]} />);
    await user.keyboard("{Escape}"); // must not throw
    expect(dialog()).toBeInTheDocument();
  });

  it("leaves the stack empty once every dialog unmounts", () => {
    expect(__testOnly.depth()).toBe(0);
    const a = render(<Harness controls={["one"]} />);
    expect(__testOnly.depth()).toBe(1);
    const b = render(<Harness controls={["two"]} label="Second" />);
    expect(__testOnly.depth()).toBe(2);
    b.unmount();
    a.unmount();
    expect(__testOnly.depth()).toBe(0);
  });

  it("reports whether any modal dialog is open, for page-level shortcuts", () => {
    expect(isModalDialogOpen()).toBe(false);
    const a = render(<Harness controls={["one"]} />);
    expect(isModalDialogOpen()).toBe(true);
    a.unmount();
    expect(isModalDialogOpen()).toBe(false);
  });

  /**
   * Stacking, and the reason the stack replaced an `enabled` flag.
   */
  it("only the TOP-most dialog reacts; the one underneath defers", async () => {
    const user = userEvent.setup();
    const onLower = vi.fn();
    render(<Harness controls={["lower-1", "lower-2"]} onEscape={onLower} />);
    const upper = render(<Harness controls={["upper-1", "upper-2"]} label="Upper" />);

    screen.getByText("upper-2").focus();
    await user.tab();
    // Wrapped inside the UPPER dialog, not dragged into the lower one.
    expect(activeText()).toBe("upper-1");
    await user.keyboard("{Escape}");
    expect(onLower).not.toHaveBeenCalled();
    upper.unmount();
  });

  it("hands control BACK to the dialog underneath when the top one unmounts", async () => {
    const user = userEvent.setup();
    const onLower = vi.fn();
    render(<Harness controls={["lower-1", "lower-2"]} onEscape={onLower} />);
    const upper = render(<Harness controls={["upper-1"]} label="Upper" />);
    upper.unmount();

    screen.getByText("lower-2").focus();
    await user.tab();
    expect(activeText()).toBe("lower-1");
    await user.keyboard("{Escape}");
    expect(onLower).toHaveBeenCalledTimes(1);
  });

  /**
   * THE COLD-WINDOW REGRESSION. The previous design took `enabled` and
   * `ConnectedAccountsDrawer` passed `!passwordOpen`, so the drawer stood down
   * the instant the modal was REQUESTED — while the modal is `React.lazy` and
   * had not mounted yet. Review measured that window: Tab was not prevented and
   * focus stayed on a page button behind the backdrop.
   *
   * Keying on mount instead makes the state unrepresentable: until something
   * else actually mounts, this dialog is still the top of the stack.
   *
   * RED if the hook ever regains a caller-driven stand-down flag.
   */
  it("keeps trapping while a dialog above it is only REQUESTED, not yet mounted", async () => {
    const user = userEvent.setup();
    render(<Harness controls={["one", "two"]} />);
    // Nothing else has mounted — the exact state during a lazy chunk fetch.
    expect(__testOnly.depth()).toBe(1);
    screen.getByText("outside-before").focus();
    await user.tab();
    expect(activeText()).toBe("one");
  });

  /**
   * The ref refresh. `onEscape` is held in a ref so the listener effect can have
   * empty deps, but the ref must be REFRESHED each render or the very first
   * handler is frozen in forever. Mutation caught this: deleting the refresh
   * left every other test green, because all three call sites happen to pass an
   * arrow that does the same thing on every render.
   *
   * RED if `onEscapeRef.current = onEscape` stops running.
   */
  it("calls the CURRENT onEscape after a re-render, not the one from mount", async () => {
    const user = userEvent.setup();
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<Harness controls={["one"]} onEscape={first} />);
    rerender(<Harness controls={["one"]} onEscape={second} />);
    await user.keyboard("{Escape}");
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
  });

  /**
   * The listener itself must come off on unmount. The stack entry is removed
   * too, which makes a leaked listener inert — so nothing observable changes
   * and only a direct count can see it. Without this, deleting
   * `document.removeEventListener` survived every other test.
   */
  it("removes its document listener on unmount, not just its stack entry", () => {
    const add = vi.spyOn(document, "addEventListener");
    const remove = vi.spyOn(document, "removeEventListener");
    const { unmount } = render(<Harness controls={["one"]} />);
    const added = add.mock.calls.filter(([type]) => type === "keydown").map(([, fn]) => fn);
    expect(added.length).toBeGreaterThan(0);
    unmount();
    const removed = remove.mock.calls.filter(([type]) => type === "keydown").map(([, fn]) => fn);
    for (const fn of added) expect(removed).toContain(fn);
    add.mockRestore();
    remove.mockRestore();
  });

  it("stops trapping once unmounted, so the listener does not outlive the dialog", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<Harness controls={["one", "two"]} />);
    unmount();
    render(
      <>
        <button type="button">after-a</button>
        <button type="button">after-b</button>
      </>,
    );
    screen.getByText("after-a").focus();
    await user.tab();
    expect(activeText()).toBe("after-b");
  });
});
