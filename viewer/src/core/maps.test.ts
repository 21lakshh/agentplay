import { describe, expect, it, vi } from 'vitest';
import {
  fetchMapWithRetry,
  isUrlFailure,
  mapRequestIds,
} from './maps';
import {
  SESSION_MAP_SCHEMA_V1,
  sessionExplorerMapBatchSchema,
  sessionMapSchema,
  isMapFound,
} from './schema';

const LIST = ['a', 'b', 'c', 'd', 'e'];

describe('mapRequestIds', () => {
  it('asks for the selected session plus the next two', () => {
    expect(mapRequestIds(LIST, 'b')).toEqual(['b', 'c', 'd']);
  });

  it('stops at the end of the list', () => {
    expect(mapRequestIds(LIST, 'd')).toEqual(['d', 'e']);
    expect(mapRequestIds(LIST, 'e')).toEqual(['e']);
  });

  it('skips sessions already known to have no map', () => {
    expect(mapRequestIds(LIST, 'a', new Set(['b']))).toEqual(['a', 'c', 'd']);
  });

  it('requests a selected id that is not in the list on its own', () => {
    expect(mapRequestIds(LIST, 'zz')).toEqual(['zz']);
  });
});

const doc = sessionMapSchema.parse({
  schema: SESSION_MAP_SCHEMA_V1,
  session: { id: 'local:claude-code:x', startedAt: '2026-09-10T00:00:00Z' },
  totals: {},
});

class ExpiredError extends Error {
  code = 'expired';
}

describe('fetchMapWithRetry', () => {
  it('uses the first URL when it works', async () => {
    const fresh = vi.fn(async () => 'u2');
    const fetchDoc = vi.fn(async () => doc);
    await expect(fetchMapWithRetry('u1', fresh, fetchDoc)).resolves.toBe(doc);
    expect(fresh).not.toHaveBeenCalled();
    expect(fetchDoc).toHaveBeenCalledWith('u1');
  });

  it('re-resolves once after an expired URL', async () => {
    const fresh = vi.fn(async () => 'u2');
    const fetchDoc = vi
      .fn<(url: string) => Promise<typeof doc>>()
      .mockRejectedValueOnce(new ExpiredError('403'))
      .mockResolvedValueOnce(doc);
    await expect(fetchMapWithRetry('u1', fresh, fetchDoc)).resolves.toBe(doc);
    expect(fresh).toHaveBeenCalledTimes(1);
    expect(fetchDoc.mock.calls.map((c) => c[0])).toEqual(['u1', 'u2']);
  });

  it('re-resolves once after a network failure, then gives up', async () => {
    const fresh = vi.fn(async () => 'u2');
    const fetchDoc = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    });
    await expect(fetchMapWithRetry('u1', fresh, fetchDoc)).rejects.toThrow(
      TypeError
    );
    expect(fresh).toHaveBeenCalledTimes(1);
    expect(fetchDoc).toHaveBeenCalledTimes(2);
  });

  it('does not re-resolve for other failures (e.g. an unsupported schema)', async () => {
    const fresh = vi.fn(async () => 'u2');
    const err = Object.assign(new Error('bad'), { code: 'unsupported_schema' });
    const fetchDoc = vi.fn(async () => {
      throw err;
    });
    await expect(fetchMapWithRetry('u1', fresh, fetchDoc)).rejects.toBe(err);
    expect(fresh).not.toHaveBeenCalled();
    expect(isUrlFailure(err)).toBe(false);
  });
});

describe('sessionExplorerMapBatchSchema', () => {
  it('parses signed and not_found items', () => {
    const b = sessionExplorerMapBatchSchema.parse({
      success: true,
      expiresIn: 300,
      maps: [
        {
          sessionId: 'a',
          url: 'https://s3/x',
          schemaVersion: 1,
          contentHash: 'h',
          bytesGz: 10,
        },
        { sessionId: 'b', status: 'not_found' },
      ],
    });
    expect(b.maps.map((m) => isMapFound(m))).toEqual([true, false]);
  });
});
