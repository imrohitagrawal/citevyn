/**
 * The shared-answer calls (ADR-0005 Phase 6C; docs/API_SPEC.md "Shareable
 * answers"). Used only by lazy components: the answer actions and the Shared
 * links drawer.
 */
import { apiFetch } from "./api";

export interface ShareView {
  share_id: string;
  /** Always `/s/<32 hex>`; see {@link shareLink}. */
  url: string;
  /** The question, cut to 200 characters by the server. */
  question: string;
  created_at: string;
}

/**
 * The full link for a share, or null if the server sent anything but
 * `/s/<32 hex>`. The link is only ever built from the id, never from free text.
 */
export function shareLink(share: Pick<ShareView, "url">, origin: string): string | null {
  return /^\/s\/[0-9a-f]{32}$/.test(share.url) ? `${origin}${share.url}` : null;
}

/** Share one of your answers. Sharing it again returns the same link. */
export function shareAnswer(sessionId: string, messageId: string): Promise<ShareView> {
  return apiFetch<ShareView>(`/v1/sessions/${sessionId}/messages/${messageId}/share`, {
    method: "POST",
  });
}

export async function listShares(): Promise<ShareView[]> {
  return (await apiFetch<{ shares: ShareView[] }>("/v1/me/shares")).shares;
}

export async function revokeShare(shareId: string): Promise<void> {
  await apiFetch<unknown>(`/v1/me/shares/${shareId}`, { method: "DELETE" });
}
