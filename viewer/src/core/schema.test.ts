import { describe, expect, it } from 'vitest';
import { sessionExplorerSessionSchema, sessionMapSchema } from './schema';

const BASE = { sessionId: 's1', startedAt: '2026-09-10T00:00:00Z' };

// The CLI writes JSON, so an absent optional is `null`, not a missing key. The
// schema's optionals rejected null, and `tasks` is wrapped in `.catch(undefined)`
// — so a single `"name": null` silently dropped every task in the map and the
// sidebar fell back to "Task 1", "Task 2", ... for every turn-derived session.
describe('sessionMapSchema null handling', () => {
  const map = (tasks: unknown) => ({
    schema: 'agentplay/v1',
    session: { id: 's1', startedAt: '2026-09-10T00:00:00Z' },
    totals: {},
    events: [],
    tasks,
  });

  it('keeps a tasks block whose optional fields are null', () => {
    const parsed = sessionMapSchema.parse(
      map([
        { n: 1, id: 'turn:1', name: null, source: 'turns', startSeq: 0, endSeq: 3 },
        { n: 2, id: 'turn:2', name: 'fix the flaky test', source: 'turns' },
      ])
    );
    expect(parsed.tasks).toHaveLength(2);
    expect(parsed.tasks?.[0]?.name ?? null).toBeNull();
    expect(parsed.tasks?.[1]?.name).toBe('fix the flaky test');
  });

  it('still drops a genuinely malformed tasks block rather than failing the map', () => {
    const parsed = sessionMapSchema.parse(map([{ n: 'not-a-number' }]));
    expect(parsed.tasks).toBeUndefined();
    expect(parsed.schema).toBe('agentplay/v1');
  });
});

describe('sessionExplorerSessionSchema repos', () => {
  it('keeps plain string repos as-is', () => {
    const s = sessionExplorerSessionSchema.parse({
      ...BASE,
      repos: ['api', 'web'],
    });
    expect(s.repos).toEqual(['api', 'web']);
  });

  // A server may reduce a commit-summary repo to `{name, remote}` when it is
  // not a plain string. Parsing it with `z.array(z.string())`
  // would throw and put the whole sessions list into its error state.
  it('normalizes {name, remote} objects to their display name', () => {
    const s = sessionExplorerSessionSchema.parse({
      ...BASE,
      repos: [
        { name: 'api', remote: 'git@github.com:acme/api.git' },
        'web',
      ],
    });
    expect(s.repos).toEqual(['api', 'web']);
  });

  it('falls back to remote when name is missing', () => {
    const s = sessionExplorerSessionSchema.parse({
      ...BASE,
      repos: [{ remote: 'git@github.com:acme/api.git' }],
    });
    expect(s.repos).toEqual(['git@github.com:acme/api.git']);
  });

  it('drops entries with neither a usable name nor remote', () => {
    const s = sessionExplorerSessionSchema.parse({
      ...BASE,
      repos: [{}, '', '   '],
    });
    expect(s.repos).toEqual([]);
  });
});
