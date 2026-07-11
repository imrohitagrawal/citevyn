import { useCallback, useRef, useState } from "react";

export interface Toast {
  id: string;
  kind: "info" | "success" | "warning" | "error";
  title: string;
  message: string;
}

export function useToast() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const counter = useRef(0);

  const addToast = useCallback(
    (toast: Omit<Toast, "id">) => {
      const id = `toast-${++counter.current}`;
      setToasts((prev) => [...prev, { ...toast, id }]);

      // Auto-dismiss after 5 seconds
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, 5000);
    },
    []
  );

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, addToast, removeToast };
}
