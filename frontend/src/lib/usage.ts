/**
 * The usage-insights call (ADR-0005 Phase 6D; docs/API_SPEC.md "Usage
 * insights"). Used only by the lazy Usage drawer.
 */
import { apiFetch } from "./api";

export interface UsageDay {
  /** A UTC day, ISO `YYYY-MM-DD`. */
  date: string;
  chat: number;
  mcp: number;
}

export interface UsageInsights {
  usage: {
    kind: string;
    used: number;
    limit: number;
    remaining: number;
    resets_at: string | null;
    verified: boolean;
  };
  period_start: string;
  days: UsageDay[];
  totals: { chat: number; mcp: number; total: number };
}

export function getUsage(): Promise<UsageInsights> {
  return apiFetch<UsageInsights>("/v1/me/usage");
}
