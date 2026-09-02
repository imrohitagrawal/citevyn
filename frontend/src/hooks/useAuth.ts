/**
 * useAuth — React binding for ``lib/authStore`` (ADR-0004 PR 8).
 *
 * ``useSyncExternalStore`` is what makes a module-level store (not a
 * Context provider) safe to read from React 18's concurrent renderer —
 * it is the same primitive Redux/Zustand's React bindings use internally.
 */
import { useCallback, useEffect, useSyncExternalStore } from "react";
import {
  bootstrapAuth,
  getAuthSnapshot,
  login,
  logout,
  register,
  subscribeAuth,
} from "../lib/authStore";

export function useAuth() {
  const state = useSyncExternalStore(subscribeAuth, getAuthSnapshot, getAuthSnapshot);

  // Resolve identity once per mount of whatever top-level component calls
  // this first (LandingPage). Re-running on every consumer would refetch
  // /v1/auth/me once per component using the hook; a module-level guard
  // isn't needed because React only mounts the app shell once.
  useEffect(() => {
    void bootstrapAuth();
  }, []);

  const signIn = useCallback((email: string, password: string) => login(email, password), []);
  const signUp = useCallback((email: string, password: string) => register(email, password), []);
  const signOut = useCallback(() => logout(), []);
  // ADR-0004 PR 14's magic-link / set-password actions are deliberately NOT
  // exposed here: this hook is in the eager bundle, and their only caller
  // (the lazy AuthModal) imports them straight from authStore / api.

  return {
    status: state.status,
    user: state.user,
    isSignedIn: state.status === "signed-in",
    signIn,
    signUp,
    signOut,
  };
}
