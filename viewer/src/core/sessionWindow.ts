import { windowDays, type WindowKey } from './windowOptions';

/** Days from the 1st of `now`'s UTC month through `now`, inclusive of today.
 * UTC, not local, to match `from`/`to` below and the server's UTC `sessionDate`.
 * A calendar "days this month" count, but keyed off the caller's clock
 * instead of a fresh `new Date()` baked into a module-level constant at
 * import time: such a constant freezes at whenever the module first loaded
 * and goes stale for any session still open past a month boundary.
 *
 * The day-of-month number IS the inclusive count on purpose, not an
 * elapsed-time calculation: `now.getTime() - anchor.getTime()` undercounts
 * at exact midnight (an integer number of days has elapsed, so
 * `Math.ceil` returns it unchanged instead of adding the partial day that
 * is "today") and is sensitive to DST shifting the ms-per-day. `getUTCDate()`
 * has neither problem. */
function daysThisMonth(now: Date): number {
  return now.getUTCDate();
}

/**
 * `from` / `to` (UTC YYYY-MM-DD, inclusive) for the sessions list, from the
 * window selector. "All time" sends neither.
 */
export function sessionWindowRange(
  windowKey: WindowKey,
  now: Date
): { from?: string; to?: string } {
  const days =
    windowKey === 'mtd' ? daysThisMonth(now) : windowDays(windowKey);
  if (days == null) return {};
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  const from = new Date(now.getTime());
  from.setUTCDate(from.getUTCDate() - Math.max(days - 1, 0));
  return { from: iso(from), to: iso(now) };
}
