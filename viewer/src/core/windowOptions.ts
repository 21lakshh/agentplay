/**
 * Date-range window for the sessions list: the option list the selector
 * renders and the day count each key maps to. No React, no fetchers.
 */
export type WindowKey = 'mtd' | '30d' | '90d' | 'all';

export interface WindowOption {
  key: WindowKey;
  /** inclusive day count; `null` = all time. `mtd` is computed per call. */
  days: number | null;
  label: string;
}

export const WINDOW_OPTIONS: WindowOption[] = [
  { key: 'mtd', days: null, label: 'MTD' },
  { key: '30d', days: 30, label: 'Last 30d' },
  { key: '90d', days: 90, label: 'Last 90d' },
  { key: 'all', days: null, label: 'All time' },
];

export const WINDOW_KEYS = WINDOW_OPTIONS.map((o) => o.key);

/** Day count for a fixed window; `null` for all time. `mtd` depends on the
 * caller's clock, so `sessionWindowRange` computes it itself. */
export function windowDays(key: WindowKey): number | null {
  return WINDOW_OPTIONS.find((o) => o.key === key)?.days ?? null;
}
