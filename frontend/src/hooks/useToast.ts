import { useCallback, useEffect, useRef, useState } from "react";

export interface Toast {
  id: string;
  kind: "info" | "success" | "warning" | "error";
  title: string;
  message: string;
}

export function useToast() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const counter = useRef(0);
  const dismissTimers = useRef<ReturnType<typeof setTimeout>[]>([]);

  /**
   * The auto-dismiss timers below used to be fire-and-forget. Unmount the tree
   * inside those five seconds and they stayed queued, then called `setToasts`
   * on a component that no longer exists.
   *
   * In the browser that is a wasted wakeup. In a vitest worker it is worse: the
   * callback lands after the test environment has been torn down, and vitest
   * exits 1 while reporting every single test as passed — a required status
   * check going red with nothing in the output to point at. Reproduced on
   * unmodified `main` in 1 of 6 full-suite runs while investigating #344, so it
   * predates that fix; making the tests faster only makes the timer more likely
   * to land after the file finishes rather than during it.
   */
  useEffect(
    () => () => {
      dismissTimers.current.forEach(clearTimeout);
      dismissTimers.current = [];
    },
    [],
  );

  const addToast = useCallback(
    (toast: Omit<Toast, "id">) => {
      const id = `toast-${++counter.current}`;
      setToasts((prev) => [...prev, { ...toast, id }]);

      // Auto-dismiss after 5 seconds
      const timer = setTimeout(() => {
        dismissTimers.current = dismissTimers.current.filter((t) => t !== timer);
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 5000);
      dismissTimers.current.push(timer);
    },
    []
  );

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, addToast, removeToast };
}
