import { describe, expect, it } from 'vitest';
import {
  SESSION_MAP_SCHEMA_V1,
  sessionMapSchema,
} from './schema';
import {
  buildTree,
  errSource,
  eventNodeRefs,
  stateAt,
} from './sessionMapModel';
import {
  IDLE_CAP_MS,
  contextSeries,
  detailRows,
  indexAtFraction,
  sessionSignals,
  signalsAt,
  timePositions,
} from './sessionSignals';
import { cumulativeCost } from './sessionMapModel';
import { sessionWindowRange } from './sessionWindow';

type RawEvent = Record<string, unknown> & { kind: string; t: number };

function makeMap(events: RawEvent[], totals: Record<string, unknown> = {}) {
  return sessionMapSchema.parse({
    schema: SESSION_MAP_SCHEMA_V1,
    session: { id: 'local:claude-code:s1', startedAt: '2026-09-10T00:00:00Z' },
    totals,
    repos: [{ id: 'r0', name: 'backend' }],
    files: [{ id: 0, repo: 'r0', path: 'a.py' }],
    agents: [{ id: 'main', parent: null, kind: 'main' }],
    events: events.map((e, i) => ({ seq: i, agent: 'main', err: false, ...e })),
  });
}

const MIN = 60_000;
const EVENTS: RawEvent[] = [
  { kind: 'user', t: 0, chars: 10 }, // 0
  {
    kind: 'llm',
    t: 1000,
    in: 10,
    cw: 1000,
    cr: 9000,
    out: 50,
    cost: 0.1,
    latency: 2000,
  }, // 1 ctx 10010
  {
    kind: 'exec',
    t: 2000,
    tool: 'Bash',
    verb: 'pytest',
    err: true,
    errClass: 'exit_code',
    exitCode: 1,
    targets: [{ file: 0, touch: 'read' }],
  }, // 2
  {
    kind: 'exec',
    t: 3000,
    tool: 'Bash',
    verb: 'pytest',
    retryOf: 2,
    attempt: 2,
    targets: [{ file: 0, touch: 'read' }],
  }, // 3
  {
    kind: 'mcp',
    t: 4000,
    server: 'linear',
    tool: 'list_issues',
    err: true,
    errClass: 'not_found',
  }, // 4
  {
    kind: 'error',
    t: 5000,
    source: 'llm',
    errClass: 'rate_limit',
    err: true,
    retry: true,
  }, // 5
  { kind: 'delay', t: 5000 + 3 * MIN, cause: 'tool', ms: 3 * MIN, ref: 4 }, // 6
  {
    kind: 'llm',
    t: 5000 + 3 * MIN + 1000,
    in: 5,
    cw: 0,
    cr: 40000,
    out: 20,
    cost: 0.2,
    latency: 90_000,
    stop: 'max_tokens',
    thinking: true,
    toolCalls: 2,
    textChars: 300,
  }, // 7 ctx 40005
  {
    kind: 'compaction',
    t: 5000 + 4 * MIN,
    trigger: 'auto',
    preTokens: 50000,
    postTokens: 8000,
    ms: 4000,
  }, // 8
  { kind: 'user', t: 5000 + 64 * MIN, chars: 5 }, // 9: 60 min idle gap
  {
    kind: 'subagent',
    t: 5000 + 65 * MIN,
    subagentType: 'fork',
    err: true,
    errClass: 'subagent_failed',
  }, // 10
];

describe('errSource', () => {
  it('derives the source from the event kind or the error event', () => {
    const m = makeMap(EVENTS);
    expect(m.events.filter((e) => e.err).map(errSource)).toEqual([
      'tool',
      'mcp',
      'llm',
      'orchestration',
    ]);
    const hookErr = makeMap([
      { kind: 'error', t: 0, source: 'hook', err: true },
    ]);
    expect(errSource(hookErr.events[0])).toBe('hook');
    const unknown = makeMap([
      { kind: 'error', t: 0, source: 'mystery', err: true },
    ]);
    expect(errSource(unknown.events[0])).toBe('tool');
  });
});

describe('contextSeries', () => {
  it('carries the main context forward and drops at a compaction', () => {
    const c = contextSeries(makeMap(EVENTS));
    expect(c.values[0]).toBe(0);
    expect(c.values[1]).toBe(10010);
    expect(c.values[5]).toBe(10010); // carried
    expect(c.values[7]).toBe(40005);
    expect(c.before.get(8)).toBe(50000);
    expect(c.values[8]).toBe(8000);
    expect(c.values[10]).toBe(8000);
    expect(c.max).toBe(50000);
  });

  it('uses totals.maxContext when it is larger', () => {
    expect(contextSeries(makeMap(EVENTS, { maxContext: 90000 })).max).toBe(
      90000
    );
  });
});

describe('timePositions', () => {
  const m = makeMap(EVENTS);

  it('spaces events evenly on the steps axis', () => {
    const pos = timePositions(m.events, 'steps');
    expect(pos[0]).toBe(0);
    expect(pos[5]).toBeCloseTo(0.5);
    expect(pos[10]).toBe(1);
  });

  it('caps idle gaps (user turns, gaps over 20 min) to 5 min on the time axis', () => {
    const pos = timePositions(m.events, 'time');
    // raw gaps: 1000 x5 (0..5), 3min, 1000, 1min-1000, 60min (user) -> 5min, 1min
    const total = 5000 + 3 * MIN + 1000 + (MIN - 1000) + IDLE_CAP_MS + MIN;
    expect(pos[9]).toBeCloseTo((total - MIN) / total);
    expect(pos[10]).toBe(1);
    for (let i = 1; i < pos.length; i++)
      expect(pos[i]).toBeGreaterThanOrEqual(pos[i - 1]);
  });

  it('maps a fraction back to the nearest event', () => {
    const pos = timePositions(m.events, 'steps');
    expect(indexAtFraction(pos, 0)).toBe(0);
    expect(indexAtFraction(pos, 0.52)).toBe(5);
    expect(indexAtFraction(pos, 1)).toBe(10);
  });
});

describe('signalsAt / sessionSignals', () => {
  const m = makeMap(EVENTS);

  it('counts signals up to the playhead', () => {
    const at = signalsAt(m.events, 6);
    expect(at.errors).toBe(3);
    expect(at.bySource).toEqual({
      tool: 1,
      mcp: 1,
      llm: 1,
      hook: 0,
      orchestration: 0,
    });
    expect(at.byClass).toEqual({ exit_code: 1, not_found: 1, rate_limit: 1 });
    expect(at.retries).toBe(1);
    expect(at.delays).toBe(1);
    expect(at.delayMs).toBe(3 * MIN);
    expect(at.compactions).toBe(0);
    expect(at.ctxNow).toBe(10010);
    expect(signalsAt(m.events, 8).ctxNow).toBe(8000);
  });

  it('computes session signals from events when totals are absent', () => {
    const s = sessionSignals(m);
    expect(s.errors).toBe(4);
    expect(s.bySource.orchestration).toBe(1);
    expect(s.compactions).toBe(1);
    expect(s.maxContext).toBe(50000);
    expect(s.latencyP50).toBe(2000);
    expect(s.latencyP95).toBe(90_000);
  });

  it('prefers the CLI totals when present', () => {
    const s = sessionSignals(
      makeMap(EVENTS, {
        errors: 1,
        errorsBySource: { tool: 7 },
        retries: 9,
        llmLatencyP50: 5,
      })
    );
    // The event-derived count for EVENTS is 4 (see the test above); the CLI
    // total of 1 must win, not lose to it.
    expect(s.errors).toBe(1);
    expect(s.bySource.tool).toBe(7);
    expect(s.bySource.mcp).toBe(0);
    expect(s.retries).toBe(9);
    expect(s.latencyP50).toBe(5);
  });
});

describe('error nodes on the map', () => {
  it('lights error-class nodes and rings the node that failed', () => {
    const m = makeMap(EVENTS);
    const tree = buildTree(m);
    expect(tree.byId.get('errsrc:tool')?.parent?.id).toBe('g:errors');
    expect(tree.root.children[tree.root.children.length - 1].id).toBe(
      'g:errors'
    );
    const st = stateAt(eventNodeRefs(m, tree), 3);
    expect(st.get('errc:tool/exit_code')?.cnt).toBe(1);
    expect(st.get('file:0')?.err).toBe(1);
    expect(st.get('file:0')?.cnt).toBe(2);
  });
});

describe('detailRows', () => {
  const m = makeMap(EVENTS);
  const cum = cumulativeCost(m.events);
  const ord = new Map<string, number>();
  const asObj = (i: number, compact = false) =>
    Object.fromEntries(
      detailRows(m, i, cum, ord, compact).map((r) => [r.k, r.v])
    );

  it('shows LLM metadata without any response text', () => {
    const r = asObj(7);
    expect(r['context in']).toBe('40.0K');
    expect(r['session so far']).toBe('$0.30');
    expect(r['stop reason']).toBe('max_tokens');
    expect(r['thinking']).toBe('yes');
    expect(r['tool calls asked']).toBe('2');
    expect(r['response text']).toBe('300 chars (not stored)');
    expect(r['agent']).toBe('Main agent');
    expect(asObj(7, true)['agent']).toBeUndefined();
  });

  it('shows failures, retries, delays and compactions', () => {
    expect(asObj(2)['exit code']).toBe('1');
    expect(asObj(3)['retry']).toBe('attempt 2 · after #2');
    expect(asObj(6)['duration']).toBe('3m 00s');
    expect(asObj(8)['dropped']).toBe('42.0K');
  });
});

describe('sessionWindowRange', () => {
  const now = new Date('2026-09-22T10:00:00Z');
  it('derives inclusive UTC from/to from the page window', () => {
    expect(sessionWindowRange('30d', now)).toEqual({
      from: '2026-08-24',
      to: '2026-09-22',
    });
    expect(sessionWindowRange('90d', now).from).toBe('2026-06-25');
    expect(sessionWindowRange('all', now)).toEqual({});
  });

  describe('mtd', () => {

    // A window length computed once at import time would return the SAME
    // `from` for both months below instead of tracking `now`.
    it("derives MTD from `now`'s own month, not an import-time snapshot", () => {
      expect(
        sessionWindowRange('mtd', new Date('2026-08-31T12:00:00Z'))
      ).toEqual({
        from: '2026-08-01',
        to: '2026-08-31',
      });
      expect(
        sessionWindowRange('mtd', new Date('2026-09-05T12:00:00Z'))
      ).toEqual({
        from: '2026-09-01',
        to: '2026-09-05',
      });
    });

    // An elapsed-ms calculation undercounts at exact local midnight: zero
    // ms have passed since 00:00 on day N, so a naive `Math.ceil` gives
    // N - 1 instead of including day N itself.
    // Server `sessionDate` is UTC, so the month boundary is UTC too. The suite
    // runs under TZ=America/Los_Angeles (vite.config.ts), where this instant
    // is still Sep 30 locally.
    it('uses the UTC month, not the local one, near a month boundary', () => {
      expect(
        sessionWindowRange('mtd', new Date('2026-10-01T03:00:00Z'))
      ).toEqual({ from: '2026-10-01', to: '2026-10-01' });
      expect(
        sessionWindowRange('mtd', new Date('2026-09-30T23:59:59Z'))
      ).toEqual({ from: '2026-09-01', to: '2026-09-30' });
    });

    it('counts today even at exact local midnight', () => {
      expect(
        sessionWindowRange('mtd', new Date('2026-09-02T00:00:00Z'))
      ).toEqual({
        from: '2026-09-01',
        to: '2026-09-02',
      });
    });
  });
});

describe('detailRows for a failed hook', () => {
  const m = makeMap([
    { kind: 'exec', t: 0, tool: 'Bash', verb: 'pytest' },
    { kind: 'error', t: 1, err: true, source: 'hook', errClass: 'hook_blocking', hook: 'PostToolUse:Bash', hookEvent: 'PostToolUse', ref: 0, srcUuid: 'u1' },
    { kind: 'error', t: 2, err: true, source: 'hook', errClass: 'hook_error', hook: 'Stop', ref: 99 },
  ]);
  const rows = (i: number) => detailRows(m, i, cumulativeCost(m.events), new Map(), false);

  it('says whether it blocked and points at the call that triggered it', () => {
    expect(rows(1).map((r) => [r.k, r.v, r.seek])).toEqual([
      ['source', 'Hook', undefined],
      ['class', 'hook_blocking', undefined],
      ['hook', 'PostToolUse:Bash', undefined],
      ['blocking', 'yes', undefined],
      ['triggered by', '#0 pytest', 0],
      ['agent', 'Main agent', undefined],
    ]);
  });

  it('shows no link when the cause is not in the map', () => {
    const r = rows(2);
    expect(r.find((x) => x.k === 'blocking')?.v).toBe('no');
    expect(r.some((x) => x.k === 'triggered by')).toBe(false);
  });
});
