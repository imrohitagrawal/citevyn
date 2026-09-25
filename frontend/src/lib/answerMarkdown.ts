/**
 * "Copy answer as Markdown" and the "docs as of" dates (ADR-0005 §6).
 *
 * Pure functions, so the copied text and the stamps are tested without a DOM.
 */

/** The shape `groupSources` produces; declared here so this module stands alone. */
export interface DatedSourceGroup {
  title: string;
  url: string;
  markers: string[];
  /** The document's fetch date, ISO `YYYY-MM-DD`, when the backend knew it. */
  asOf?: string;
}

const MONTHS = "JanFebMarAprMayJunJulAugSepOctNovDec";

/**
 * `2026-09-01` -> `1 Sep 2026`. Parsed by hand rather than through `Date`, so the
 * reader's time zone can never move the day (a UTC midnight is the previous day
 * in the Americas).
 */
export function formatDocsDate(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${Number(d)} ${MONTHS.slice(Number(m) * 3 - 3, Number(m) * 3)} ${y}`;
}

/** The oldest known date: the one bound that holds for every source. ISO days sort as strings. */
export function oldestDate(dates: (string | undefined)[]): string | undefined {
  const known = dates.filter((d): d is string => !!d).sort();
  return known[0];
}

import { isSafeHref } from "./safeHref";

/** The answer, then every source it cites with its link and date. */
export function answerMarkdown(text: string, groups: DatedSourceGroup[]): string {
  if (groups.length === 0) return text;
  const asOf = oldestDate(groups.map((g) => g.asOf));
  const lines = [text, "", asOf ? `Sources (docs as of ${formatDocsDate(asOf)}):` : "Sources:"];
  for (const g of groups) {
    // Brackets in a title would end the link text early; an unsafe URL (not
    // http(s) or relative) is left out of the link, as the source card does.
    const title = g.title.replace(/[[\]]/g, "\\$&");
    const label = isSafeHref(g.url) ? `[${title}](${g.url})` : title;
    const date = g.asOf ? ` — docs as of ${formatDocsDate(g.asOf)}` : "";
    lines.push(`- [${g.markers.join(", ")}] ${label}${date}`);
  }
  return lines.join("\n");
}
