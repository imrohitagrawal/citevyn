/**
 * useSoftChat
 *
 * Self-contained chat session hook for the Softly wrapper. Owns:
 *   - lazy session creation (first send POSTs /v1/sessions)
 *   - the message list
 *   - in-flight / error / retry state
 *
 * Mirrors the surface that ChatView exposes to the brutalist
 * landing, but lives entirely inside the Softly wrapper so we
 * don't have to thread a reducer through App.tsx.
 */

import { useCallback, useState } from "react";

import { askQuestion, createSession } from "./api";
import type { AskResponse, SessionId } from "./types";
import { ApiClientError } from "./types";

import type { SoftlyChatMessage } from "../components/SoftlyApp";

const MAX_MESSAGES = 30;

export interface UseSoftChatResult {
  messages: SoftlyChatMessage[];
  sessionId: SessionId | null;
  isBusy: boolean;
  send: (text: string) => Promise<void>;
  retry: () => Promise<void>;
  reset: () => void;
}

export function useSoftChat(): UseSoftChatResult {
  const [messages, setMessages] = useState<SoftlyChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<SessionId | null>(null);
  const [isBusy, setIsBusy] = useState(false);
  const [pendingText, setPendingText] = useState<string | null>(null);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || isBusy) return;

      setIsBusy(true);
      setPendingText(trimmed);

      const userId = `u-${Date.now()}-${Math.floor(Math.random() * 1000)}`;
      const assistantId = `a-${Date.now()}-${Math.floor(Math.random() * 1000)}`;

      setMessages((prev) => {
        const next = [
          ...prev,
          { id: userId, role: "user" as const, text: trimmed },
          { id: assistantId, role: "assistant" as const, text: "", inFlight: true },
        ];
        return next.slice(-MAX_MESSAGES);
      });

      try {
        let activeSessionId = sessionId;
        if (!activeSessionId) {
          const session = await createSession();
          activeSessionId = session.session_id;
          setSessionId(activeSessionId);
        }

        const response: AskResponse = await askQuestion(activeSessionId, trimmed);

        const finalText = response.answer;
        const citations = response.citations ?? [];

        setMessages((prev) =>
          prev.map((message) =>
            message.id === assistantId
              ? { ...message, inFlight: false, text: finalText, citations }
              : message
          )
        );
      } catch (error) {
        const message =
          error instanceof ApiClientError
            ? error.message
            : "Could not reach CiteVyn.";
        setMessages((prev) =>
          prev.map((item) =>
            item.id === assistantId
              ? { ...item, inFlight: false, text: "", error: message }
              : item
          )
        );
      } finally {
        setIsBusy(false);
        setPendingText(null);
      }
    },
    [sessionId, isBusy]
  );

  const retry = useCallback(async () => {
    if (pendingText) {
      // Strip the previous assistant turn that errored so we don't duplicate.
      setMessages((prev) =>
        prev.filter((m) => !(m.role === "assistant" && m.error))
      );
      await send(pendingText);
    }
  }, [pendingText, send]);

  const reset = useCallback(() => {
    setMessages([]);
    setSessionId(null);
    setPendingText(null);
  }, []);

  return { messages, sessionId, isBusy, send, retry, reset };
}
