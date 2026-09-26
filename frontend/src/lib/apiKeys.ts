/**
 * The API key calls (ADR-0005 Phase 6A/6B-2; docs/API_SPEC.md "API keys").
 * Used only by the lazy API keys drawer.
 */
import { apiFetch } from "./api";

export interface ApiKeyView {
  key_id: string;
  name: string;
  prefix: string;
  scope: string;
  created_at: string;
  last_used_at: string | null;
}

export async function listKeys(): Promise<ApiKeyView[]> {
  return (await apiFetch<{ keys: ApiKeyView[] }>("/v1/me/api-keys")).keys;
}

/** The full key comes back ONCE, in ``key``; it is never shown again. */
export function createKey(name: string): Promise<ApiKeyView & { key: string }> {
  return apiFetch<ApiKeyView & { key: string }>("/v1/me/api-keys", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export async function revokeKey(keyId: string): Promise<void> {
  await apiFetch<unknown>(`/v1/me/api-keys/${keyId}`, { method: "DELETE" });
}
