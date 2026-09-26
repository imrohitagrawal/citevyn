/**
 * What the server says the web client should show (ADR-0005 Phase 5,
 * ``GET /v1/config``): whether the access model is on, and whether sign-up
 * needs the proof-of-work bot check.
 *
 * Fetched once at start-up, only in live mode (a demo build has no backend).
 * Anything but a clear "on" is OFF: a failed or odd response leaves the page
 * exactly as it is today. Read synchronously with ``getClientConfig`` at the
 * moment a decision is made; ``subscribeClientConfig`` lets a component
 * re-render when it arrives.
 */
import { apiFetch, isLiveMode } from "./api";

export interface ClientConfig {
  access_model: boolean;
  bot_check: { kind: string } | null;
}

const OFF: ClientConfig = { access_model: false, bot_check: null };
let snapshot: ClientConfig = OFF;
let loading: Promise<ClientConfig> | null = null;
const listeners = new Set<() => void>();

export function getClientConfig(): ClientConfig {
  return snapshot;
}

export function subscribeClientConfig(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function loadClientConfig(): Promise<ClientConfig> {
  if (!isLiveMode()) return Promise.resolve(OFF);
  // Any failure (network, a non-object body, a throw) means OFF: today's page.
  loading ??= Promise.resolve()
    .then(() => apiFetch<Partial<ClientConfig> | null>("/v1/config"))
    .then((c) => {
      snapshot =
        c?.access_model === true ? { access_model: true, bot_check: c.bot_check ?? null } : OFF;
      listeners.forEach((l) => l());
      return snapshot;
    })
    .catch(() => OFF);
  return loading;
}

/** Tests only: forget what was loaded. */
export function resetClientConfig(): void {
  snapshot = OFF;
  loading = null;
}
