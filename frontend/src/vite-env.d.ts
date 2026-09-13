/// <reference types="vite/client" />

/**
 * Typed view of the ``VITE_*`` environment the client reads.
 *
 * These MUST stay in lockstep with the variables consumed in
 * ``src/lib/api.ts`` (base URL, client token, demo user id) and the
 * live/demo toggle read in ``src/hooks/useLandingState.ts``. Every
 * field is optional because Vite only injects the ones present in
 * the active ``.env`` file; the client supplies safe defaults.
 * See ``.env.example`` for documentation and defaults.
 */
interface ImportMetaEnv {
  /** Backend base URL. Empty in dev → Vite proxies ``/v1`` + ``/health``. */
  readonly VITE_API_BASE_URL?: string;
  /** Public client token; mirrors the backend ``CITEVYN_PUBLIC_CLIENT_TOKEN``. */
  readonly VITE_PUBLIC_CLIENT_TOKEN?: string;
  /**
   * DEPRECATED spelling of the above, still read during the #430 migration.
   * Removed once the Fly secret and the deploy build argument have moved.
   */
  readonly VITE_API_DEMO_KEY?: string;
  /** Default ``user_id`` used when creating a session. */
  readonly VITE_API_DEMO_USER_ID?: string;
  /** ``"true"`` switches the chat off canned answers onto the real backend. */
  readonly VITE_API_LIVE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
