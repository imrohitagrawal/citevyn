import "@testing-library/jest-dom/vitest";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { useFocusTrap } from "./useFocusTrap";

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
  enabled = true,
  onEscape,
}: {
  controls: string[];
  enabled?: boolean;
  onEscape?: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(ref, { onEscape, enabled });
  return (
    <>
      <button type="button">outside-before</button>
      <div ref={ref} role="dialog" aria-modal="true" aria-label="Harness" tabIndex={-1}>
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

  // The stacking case: a dialog opened ON TOP of this one arms its own trap,
  // and two competing document-level traps fight over focus.
  it("does nothing at all when disabled", async () => {
    const user = userEvent.setup();
    const onEscape = vi.fn();
    render(<Harness controls={["one", "two"]} enabled={false} onEscape={onEscape} />);
    screen.getByText("two").focus();
    await user.tab();
    // Free to leave — that is the point of standing down.
    expect(activeText()).toBe("outside-after");
    await user.keyboard("{Escape}");
    expect(onEscape).not.toHaveBeenCalled();
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
