/**
 * Session map loading policy, kept pure so it is unit-tested in
 * `maps.test.ts`:
 *  - which ids to ask the batch `map/` endpoint for (selected + next 2),
 *  - fetching a map URL with one fresh-URL retry on expiry / network
 *    failure (a defensive retry for sources whose URLs can expire;
 *    a local server's relative URLs never do).
 */
import type { SessionMap } from './schema';

/** How many sessions after the selected one to resolve and prefetch. */
export const MAP_PREFETCH_AHEAD = 2;

/**
 * Ids for one batch `map/` call: the selected session first, then the next
 * `ahead` sessions in list order, skipping ids already known to have no map.
 * A selected id that is not in the (filtered) list is still requested alone.
 */
export function mapRequestIds(
  listIds: readonly string[],
  selectedId: string,
  unavailable: ReadonlySet<string> = new Set(),
  ahead = MAP_PREFETCH_AHEAD
): string[] {
  const out = [selectedId];
  const start = listIds.indexOf(selectedId);
  if (start < 0) return out;
  for (let i = start + 1; i < listIds.length && out.length <= ahead; i++) {
    const id = listIds[i];
    if (!unavailable.has(id) && !out.includes(id)) out.push(id);
  }
  return out;
}

/** True for failures a fresh URL can fix: a 403 (URL
 * expired, for sources that issue expiring URLs) or a network-level fetch failure. */
export function isUrlFailure(err: unknown): boolean {
  if (err instanceof TypeError) return true;
  return (
    !!err &&
    typeof err === 'object' &&
    'code' in err &&
    (err as { code: unknown }).code === 'expired'
  );
}

/**
 * Fetch a map document from `url`; if that fails because the URL expired or
 * the network dropped, ask the source for a fresh URL once and try again. Any other
 * failure, or a second failure, is thrown to the caller (inline error card).
 */
export async function fetchMapWithRetry(
  url: string,
  freshUrl: () => Promise<string>,
  fetchDoc: (url: string) => Promise<SessionMap>
): Promise<SessionMap> {
  try {
    return await fetchDoc(url);
  } catch (err) {
    if (!isUrlFailure(err)) throw err;
    return fetchDoc(await freshUrl());
  }
}
