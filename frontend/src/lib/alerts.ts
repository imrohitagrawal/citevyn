/**
 * The weekly digest and watch calls (ADR-0005 Phase 7; docs/API_SPEC.md "The
 * weekly digest" and "Watches"). Used only by the lazy Alerts drawer.
 */
import { apiFetch } from "./api";

export interface DigestState {
  subscribed: boolean;
  verified: boolean;
}

export interface WatchView {
  watch_id: string;
  kind: "page" | "term";
  value: string;
  label: string;
  created_at: string;
}

export interface WatchablePage {
  url: string;
  title: string;
  product: string;
}

export function getDigest(): Promise<DigestState> {
  return apiFetch<DigestState>("/v1/me/digest");
}

export function setDigest(subscribed: boolean): Promise<DigestState> {
  return apiFetch<DigestState>("/v1/me/digest", {
    method: "PUT",
    body: JSON.stringify({ subscribed }),
  });
}

export function listWatches(): Promise<{ watches: WatchView[]; pages: WatchablePage[] }> {
  return apiFetch<{ watches: WatchView[]; pages: WatchablePage[] }>("/v1/me/watches");
}

export function addWatch(kind: "page" | "term", value: string): Promise<WatchView> {
  return apiFetch<WatchView>("/v1/me/watches", {
    method: "POST",
    body: JSON.stringify({ kind, value }),
  });
}

export async function removeWatch(watchId: string): Promise<void> {
  await apiFetch<unknown>(`/v1/me/watches/${encodeURIComponent(watchId)}`, { method: "DELETE" });
}
