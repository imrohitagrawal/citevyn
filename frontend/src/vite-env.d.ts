/// <reference types="vite/client" />

/**
 * Typed view of the ``VITE_*`` environment the client reads.
 *
 * These MUST stay in lockstep with the variables consumed in
 * ``src/lib/api.ts`` (base URL, demo key, demo user id) and the
 * live/demo toggle read in ``src/hooks/useLandingState.ts``. Every
 * field is optional because Vite only injects the ones present in
 * the active ``.env`` file; the client supplies safe defaults.
 * See ``.env.example`` for documentation and defaults.
 */
interface ImportMetaEnv {
  /** Backend base URL. Empty in dev → Vite proxies ``/v1`` + ``/health``. */
  readonly VITE_API_BASE_URL?: string;
  /** Demo bearer token; mirrors the backend ``CITEVYN_DEMO_API_KEY``. */
  readonly VITE_API_DEMO_KEY?: string;
  /** Default ``user_id`` used when creating a session. */
  readonly VITE_API_DEMO_USER_ID?: string;
  /** ``"true"`` switches the chat off canned answers onto the real backend. */
  readonly VITE_API_LIVE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
