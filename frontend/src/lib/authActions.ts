/**
 * authActions — the wire calls only a LAZY surface needs (ADR-0004 PR 14).
 *
 * ``requestMagicLink`` and ``updatePassword`` are imported solely by the
 * lazy-loaded ``AuthModal``, so keeping them here (not in ``api.ts`` /
 * ``authStore.ts``, both eager) lets Vite bundle them into the modal's own
 * chunk: the eager bundle sits within ~0.1 kB of its 63.5 kB gzip ceiling
 * (see ``docs/BACKLOG.md`` #270), and every visitor should not pay for
 * forms most never open. Same ``apiFetch`` transport and the same
 * identity-application path (``authStore.applyIdentityFrom``) as the eager
 * calls -- only the module boundary differs.
 */
import { apiFetch } from "./api";
import { applyIdentityFrom } from "./authStore";
import type { AuthUserResponse, PasswordUpdateRequest } from "./types";

/**
 * Always 202 whether or not the address is registered (the server never
 * reveals which), so the caller's only signal is "sent".
 */
export async function requestMagicLink(email: string): Promise<void> {
  await apiFetch<unknown>("/v1/auth/magic-link/request", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

/**
 * The server decides whether ``currentPassword`` is required (from the
 * stored account, never from this body); this just forwards it when given
 * and applies the refreshed identity (``has_password`` now true) on success.
 */
export async function updatePassword(newPassword: string, currentPassword?: string): Promise<void> {
  const body: PasswordUpdateRequest =
    currentPassword === undefined
      ? { new_password: newPassword }
      : { current_password: currentPassword, new_password: newPassword };
  await applyIdentityFrom(
    apiFetch<AuthUserResponse>("/v1/auth/me/password", { method: "POST", body: JSON.stringify(body) }),
  );
}
