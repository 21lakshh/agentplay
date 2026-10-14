import { describe, expect, it } from 'vitest';
import {
  SESSION_MAP_SCHEMA_V1,
  sessionMapSchema,
  type SessionMap,
} from './schema';
import {
  INNER_MAX,
  INNER_MIN,
  activeSet,
  agentLabel,
  buildTree,
  countersAt,
  cumulativeCost,
  deriveLensDoc,
  describeEvent,
  eventNodeRefs,
  fileCoverage,
  findNext,
  initialPlayhead,
  lastTouch,
  layoutTree,
  nextTouchOf,
  recentTargets,
  recomputeTotals,
  reviewMetrics,
  shiftStartedAt,
  stateAt,
} from './sessionMapModel';

type RawEvent = Record<string, unknown> & { kind: string };

function makeMap(events: RawEvent[], overrides: Record<string, unknown> = {}) {
  return sessionMapSchema.parse({
    schema: SESSION_MAP_SCHEMA_V1,
    session: {
      id: 'local:claude-code:abc12345-0000',
      agentTool: 'claude-code',
      startedAt: '2026-09-10T01:00:00Z',
      durationMs: 60_000,
      models: ['claude-opus-5'],
    },
    totals: { calls: 0, requests: 0 },
    repos: [
      { id: 'r0', name: 'backend', remote: 'org/backend' },
      { id: 'r1', name: 'cli' },
    ],
    files: [
      {
        id: 0,
        repo: 'r0',
        path: 'billing_core/deep/nested/controller.py',
        reads: 1,
        edits: 3,
      },
      {
        id: 1,
        repo: 'r0',
        path: 'billing_core/views.py',
        reads: 2,
        edits: 0,
      },
      { id: 2, repo: 'r1', path: 'README.md', reads: 0, edits: 1 },
      {
        id: 3,
        repo: 'r0',
        path: 'billing_core/new_file.py',
        reads: 0,
        edits: 1,
      },
    ],
    agents: [
      { id: 'main', parent: null, kind: 'main', cost: 3, calls: 5 },
      {
        id: 'sa-ae824d54211f',
        parent: 'main',
        kind: 'subagent',
        subagentType: 'general-purpose',
        cost: 1,
        calls: 2,
      },
    ],
    events: events.map((e, i) => ({
      seq: i,
      t: i * 1000,
      agent: 'main',
      err: false,
      ...e,
    })),
    ...overrides,
  });
}

const EVENTS: RawEvent[] = [
  { kind: 'user', chars: 120 }, // 0
  {
    kind: 'llm',
    model: 'claude-opus-5',
    in: 10,
    cw: 100,
    cr: 1000,
    out: 50,
    cost: 0.5,
  }, // 1
  { kind: 'read', tool: 'Read', targets: [{ file: 1, touch: 'read' }] }, // 2
  { kind: 'edit', tool: 'Edit', targets: [{ file: 0, touch: 'edit' }] }, // 3 blind
  { kind: 'read', tool: 'Read', targets: [{ file: 0, touch: 'read' }] }, // 4
  { kind: 'edit', tool: 'Write', targets: [{ file: 3, touch: 'edit' }] }, // 5 creates, not blind
  { kind: 'mcp', server: 'acme-context', tool: 'find_expert' }, // 6
  { kind: 'skill', skill: 'artifact-design' }, // 7
  { kind: 'web', tool: 'WebFetch', host: 'docs.example.com' }, // 8
  {
    kind: 'subagent',
    tool: 'Agent',
    subagentType: 'general-purpose',
    child: 'sa-ae824d54211f',
  }, // 9
  {
    kind: 'read',
    tool: 'Read',
    agent: 'sa-ae824d54211f',
    targets: [{ file: 1, touch: 'read' }],
  }, // 10 re-read
  {
    kind: 'exec',
    tool: 'Bash',
    verb: 'git log',
    err: true,
    targets: [{ scope: 'outside', label: '/tmp (scratch)', touch: 'read' }],
  }, // 11
  { kind: 'verify', tool: 'Bash', verb: 'pytest' }, // 12
  { kind: 'edit', tool: 'Edit', targets: [{ file: 0, touch: 'edit' }] }, // 13 after verify
  {
    kind: 'llm',
    model: 'claude-sonnet-5',
    in: 5,
    cw: 0,
    cr: 500,
    out: 20,
    cost: 0.25,
  }, // 14
  { kind: 'edit', tool: 'Edit', targets: [{ file: 2, touch: 'edit' }] }, // 15 blind (never read)
];

describe('sessionMapSchema', () => {
  it('rejects a map that is not v1', () => {
    const r = sessionMapSchema.safeParse({
      schema: 'agentplay/v2',
      session: { id: 'x', startedAt: '2026-01-01T00:00:00Z' },
      totals: {},
    });
    expect(r.success).toBe(false);
  });

  it('keeps unknown fields and defaults optional ones', () => {
    const m = makeMap([{ kind: 'read', futureField: 7 }], { extra: true });
    expect(m.events[0].agent).toBe('main');
    expect((m.events[0] as Record<string, unknown>).futureField).toBe(7);
    expect((m as Record<string, unknown>).extra).toBe(true);
    expect(m.session.truncated).toBe(false);
  });
});

describe('buildTree', () => {
  const map = makeMap(EVENTS);
  const tree = buildTree(map);

  it('puts repos first, then the named groups', () => {
    const names = tree.root.children.map((c) => `${c.type}:${c.name}`);
    expect(names.slice(0, 2)).toEqual(['repo:backend', 'repo:cli']);
    expect(names).toEqual(
      expect.arrayContaining([
        'group:outside repos',
        'group:MCP',
        'group:skills',
        'group:web',
        'group:subagents',
      ])
    );
  });

  it('collapses single-child directory chains and keeps folded ids resolvable', () => {
    const collapsed = tree.byId.get('dir:r0/billing_core/deep');
    expect(collapsed?.name).toBe('deep/nested');
    expect(tree.byId.get('dir:r0/billing_core/deep/nested')).toBe(collapsed);
    // billing_core has 3 children (deep/nested, views.py, new_file.py): not collapsed
    expect(tree.byId.get('dir:r0/billing_core')?.name).toBe('billing_core');
  });

  it('nests MCP tools under their server and labels subagents by type', () => {
    const tool = tree.byId.get('mcp:acme-context/find_expert');
    expect(tool?.parent?.id).toBe('srv:acme-context');
    expect(tool?.parent?.parent?.id).toBe('g:mcp');
    expect(tree.byId.get('agent:sa-ae824d54211f')?.name).toBe(
      'Sub 1: general-purpose'
    );
  });
});

describe('layoutTree', () => {
  it('puts leaves on the outer ring and internals between 0.32R and 0.78R', () => {
    const R = 200;
    const tree = buildTree(makeMap(EVENTS));
    const nodes = layoutTree(tree, R);
    for (const n of nodes) {
      if (n === tree.root) {
        expect(n.r).toBe(0);
      } else if (!n.children.length) {
        expect(n.r).toBeCloseTo(R);
        expect(Math.hypot(n.x, n.y)).toBeCloseTo(R);
      } else {
        expect(n.r).toBeGreaterThanOrEqual(R * INNER_MIN - 1e-9);
        expect(n.r).toBeLessThanOrEqual(R * INNER_MAX + 1e-9);
      }
    }
  });
});

describe('playhead state', () => {
  const map = makeMap(EVENTS);
  const tree = buildTree(map);
  const refs = eventNodeRefs(map, tree);

  it('counts touches and keeps the highest touch level', () => {
    const st = stateAt(refs, 4);
    const f0 = st.get('file:0');
    expect(f0?.cnt).toBe(2);
    expect(f0?.lvl).toBe(3);
    expect(f0?.first).toBe(3);
    expect(st.get('file:1')?.lvl).toBe(2);
    expect(st.has('file:2')).toBe(false);
  });

  it('keeps only the most recent distinct nodes active', () => {
    const active = activeSet(refs, 11, 3);
    // #11 failed: its place and the error-class node are both active
    expect([...active]).toEqual([
      'place:/tmp (scratch)',
      'errc:tool/other',
      'file:1',
    ]);
  });

  it('builds the trail oldest first', () => {
    const trail = recentTargets(refs, 5, 9);
    expect(trail.map((t) => t.i)).toEqual([2, 3, 4, 5]);
  });

  it('finds the one step the playhead lights: the last real touch', () => {
    expect(lastTouch(refs, 5)).toEqual({ i: 5, ids: ['file:3'] });
    // #12 (a verify) touches nothing: the failed #11 stays lit, with its
    // error class
    expect(lastTouch(refs, 12)).toEqual({
      i: 11,
      ids: ['place:/tmp (scratch)', 'errc:tool/other'],
    });
    // #1's model ref is passive, and #0 touches nothing
    expect(lastTouch(refs, 1)).toBeNull();
  });

  it('jumps to the next touch of a node or anything under it, wrapping', () => {
    expect(nextTouchOf(tree, refs, 3, 'file:0')).toBe(4);
    expect(nextTouchOf(tree, refs, 13, 'file:0')).toBe(3);
    expect(nextTouchOf(tree, refs, 0, 'dir:r0/billing_core')).toBe(2);
    expect(nextTouchOf(tree, refs, 0, 'nope')).toBeNull();
  });
});

describe('navigation helpers', () => {
  const map = makeMap(EVENTS);

  it('findNext wraps around and handles errors', () => {
    expect(findNext(map.events, 3, 'edit')).toBe(5);
    expect(findNext(map.events, 15, 'edit')).toBe(3);
    expect(findNext(map.events, 0, 'err')).toBe(11);
    expect(findNext(map.events, 0, 'subagent')).toBe(9);
    expect(findNext(makeMap([{ kind: 'read' }]).events, 0, 'user')).toBeNull();
  });

  it('opens just past the first edit, or 40% in without edits', () => {
    expect(initialPlayhead(map.events)).toBe(15);
    const noEdits = makeMap(
      Array.from({ length: 10 }, () => ({ kind: 'read' }))
    );
    expect(initialPlayhead(noEdits.events)).toBe(4);
    expect(initialPlayhead([])).toBe(0);
  });
});

describe('counters and costs', () => {
  const map = makeMap(EVENTS);

  it('counts calls, requests and files at the playhead', () => {
    const c = countersAt(map.events, 11);
    expect(c.requests).toBe(1);
    expect(c.tokens).toBe(1160);
    expect(c.cost).toBeCloseTo(0.5);
    expect(c.calls).toBe(10);
    expect(c.errors).toBe(1);
    expect(c.mcp).toBe(1);
    expect(c.skills).toBe(1);
    expect(c.subagents).toBe(1);
    expect(c.filesRead).toBe(2);
    expect(c.filesEdited).toBe(2);
  });

  it('accumulates cost as a prefix sum', () => {
    const cum = cumulativeCost(map.events);
    expect(cum[0]).toBe(0);
    expect(cum[1]).toBeCloseTo(0.5);
    expect(cum[cum.length - 1]).toBeCloseTo(0.75);
  });
});

describe('reviewMetrics', () => {
  it('flags blind edits but not a Write that creates a file', () => {
    const r = reviewMetrics(makeMap(EVENTS));
    expect(r.blindEdits).toBe(2); // #3 file 0, #15 file 2
    expect(r.errors).toBe(1);
    expect(r.churnFiles).toBe(1);
    // 3 file reads; #10 re-reads file 1 unchanged since #2
    expect(r.reReadPct).toBe(33);
    expect(r.verification).toBe('edits_after_verify');
  });

  it('reports verification after the final edit, and never', () => {
    const after = makeMap([
      { kind: 'edit', tool: 'Write', targets: [{ file: 3, touch: 'edit' }] },
      { kind: 'verify', tool: 'Bash', verb: 'pytest' },
    ]);
    expect(reviewMetrics(after).verification).toBe('after_final_edit');
    expect(reviewMetrics(after).blindEdits).toBe(0);
    expect(reviewMetrics(makeMap([{ kind: 'read' }])).verification).toBe(
      'never'
    );
  });

  it('does not count a read after an edit as a re-read', () => {
    const m = makeMap([
      { kind: 'read', tool: 'Read', targets: [{ file: 1, touch: 'read' }] },
      { kind: 'edit', tool: 'Edit', targets: [{ file: 1, touch: 'edit' }] },
      { kind: 'read', tool: 'Read', targets: [{ file: 1, touch: 'read' }] },
    ]);
    expect(reviewMetrics(m).reReadPct).toBe(0);
  });
});

describe('describeEvent and labels', () => {
  const map: SessionMap = makeMap(EVENTS);

  it('describes LLM, user and file events without leaking content', () => {
    expect(describeEvent(map, map.events[1]).title).toBe(
      'claude-opus-5 · $0.50'
    );
    expect(describeEvent(map, map.events[0]).detail).toBe(
      '120 chars (text not stored)'
    );
    const edit = describeEvent(map, map.events[3]);
    expect(edit.title).toBe('controller.py');
    expect(edit.detail).toBe(
      'backend/billing_core/deep/nested/controller.py'
    );
    expect(describeEvent(map, map.events[11]).title).toBe('/tmp (scratch)');
    expect(describeEvent(map, map.events[9]).detail).toBe(
      'spawned Sub 1: general-purpose'
    );
  });

  it('labels agents', () => {
    expect(agentLabel(map, 'main')).toBe('Main agent');
    expect(agentLabel(map, 'sa-ae824d54211f')).toBe('Sub 1: general-purpose');
  });

  it('summarises file coverage', () => {
    expect(fileCoverage(map)).toEqual({ edited: 3, read: 1, seen: 0 });
  });
});

describe('agent lens (deriveLensDoc / recomputeTotals)', () => {
  // `main` touches file 0 and file 2; `sa-1` (spawned by main at #2) has its
  // own lane: a failed exec, a retry of it, a tool delay caused by
  // it, a file it creates (file 1), and a confirmed PR create.
  const LENS_EVENTS: RawEvent[] = [
    { kind: 'user', chars: 10 }, // 0 main
    {
      kind: 'edit',
      tool: 'Write',
      targets: [{ file: 0, touch: 'edit', op: 'modify' }],
    }, // 1 main touches file 0
    {
      kind: 'subagent',
      tool: 'Agent',
      subagentType: 'general-purpose',
      child: 'sa-1',
    }, // 2 main spawns sa-1
    {
      kind: 'exec',
      tool: 'Bash',
      verb: 'pytest',
      agent: 'sa-1',
      err: true,
      errClass: 'exit_code',
    }, // 3 sa-1 fails
    { kind: 'exec', tool: 'Bash', verb: 'pytest', agent: 'sa-1', retryOf: 3 }, // 4 sa-1 retries #3
    { kind: 'delay', agent: 'sa-1', cause: 'tool', ms: 45_000, ref: 3 }, // 5 sa-1 delay caused by #3
    {
      kind: 'edit',
      tool: 'Write',
      agent: 'sa-1',
      targets: [{ file: 1, touch: 'edit', op: 'create' }],
    }, // 6 sa-1 creates file 1
    {
      kind: 'exec',
      tool: 'gh',
      verb: 'pr create',
      agent: 'sa-1',
      vcs: {
        provider: 'github',
        action: 'pr_create',
        repo: 'org/backend',
        number: 42,
      },
    }, // 7 sa-1 opens a PR
    {
      kind: 'edit',
      tool: 'Write',
      targets: [{ file: 2, touch: 'edit', op: 'modify' }],
    }, // 8 main touches file 2 (sa-1 never touches this file)
  ];
  const lensMap = makeMap(LENS_EVENTS, {
    prs: [
      {
        provider: 'github',
        repo: 'org/backend',
        number: 42,
        url: 'https://github.com/org/backend/pull/42',
        seq: 7,
      },
    ],
    files: [
      { id: 0, repo: 'r0', path: 'a.py' },
      { id: 1, repo: 'r0', path: 'b.py' },
      { id: 2, repo: 'r0', path: 'c.py' },
    ],
    agents: [
      { id: 'main', parent: null, kind: 'main', cost: 3, calls: 5 },
      {
        id: 'sa-1',
        parent: 'main',
        kind: 'subagent',
        subagentType: 'general-purpose',
        cost: 2,
        calls: 4,
      },
    ],
  });

  it('returns the full doc unchanged for "main"', () => {
    expect(deriveLensDoc(lensMap, 'main')).toBe(lensMap);
  });

  it('returns the full doc unchanged for an agent with no events of its own', () => {
    expect(deriveLensDoc(lensMap, 'nope')).toBe(lensMap);
  });

  it('reindexes events to a local 0-based sequence rebased at t=0', () => {
    const lens = deriveLensDoc(lensMap, 'sa-1');
    expect(lens.events.map((e) => e.seq)).toEqual([0, 1, 2, 3, 4]);
    expect(lens.events[0]?.t).toBe(0);
    expect(lens.events.map((e) => e.t)).toEqual([0, 1000, 2000, 3000, 4000]);
    expect(lens.events.every((e) => e.agent === 'main')).toBe(true);
  });

  it('remaps retryOf/ref to the new local sequence numbers', () => {
    const lens = deriveLensDoc(lensMap, 'sa-1');
    // event #4 (retryOf: 3) is local index 1; its retryOf must point at
    // local index 0 (the original #3), not the stale original seq.
    expect(lens.events[1]?.retryOf).toBe(0);
    // event #5 (ref: 3) is local index 2; same remap for `ref`.
    expect(lens.events[2]?.ref).toBe(0);
  });

  it('remaps retryOf/ref by original position even when events omit seq', () => {
    // `seq` is schema-optional; the remap must key off each event's own
    // position in `full.events`, not a `seq` field that may be absent.
    const noSeqMap = makeMap(
      LENS_EVENTS.map((e) => ({ ...e, seq: undefined })),
      {
        prs: lensMap.prs,
        files: lensMap.files,
        agents: lensMap.agents,
      }
    );
    expect(noSeqMap.events.every((e) => e.seq === undefined)).toBe(true);
    const lens = deriveLensDoc(noSeqMap, 'sa-1');
    expect(lens.events[1]?.retryOf).toBe(0);
    expect(lens.events[2]?.ref).toBe(0);
  });

  it("scopes totals to the lens's own events, not the full session", () => {
    const lens = deriveLensDoc(lensMap, 'sa-1');
    expect(lens.totals).toEqual(recomputeTotals(lens.events));
    expect(lens.totals.errors).toBe(1);
    expect(lens.totals.retries).toBe(1);
    expect(lens.totals.delays).toBe(1);
    expect(lens.totals.delayMs).toBe(45_000);
    expect(lens.totals.prsCreated).toBe(1);
    expect(lens.totals.filesCreated).toBe(1);
    expect(lens.totals.filesTouched).toBe(1);
    expect(lens.totals.subagents).toBe(0);
  });

  it('carries only PRs created within the lens, and derives its outcome', () => {
    const lens = deriveLensDoc(lensMap, 'sa-1');
    expect(lens.prs).toEqual(lensMap.prs);
    expect(lens.session.outcome).toBe('pr_created');
  });

  it('exposes itself as the sole "main" agent, with no further nesting', () => {
    const lens = deriveLensDoc(lensMap, 'sa-1');
    expect(lens.agents).toHaveLength(1);
    expect(lens.agents[0]?.id).toBe('main');
    expect(lens.agents[0]?.subagentType).toBe('general-purpose');
  });

  it(
    "does not leak the shared files[] aggregate into the lens's coverage " +
      "(file-touch counts must come from the lens's own " +
      'events, not the full-document file records)',
    () => {
      const lens = deriveLensDoc(lensMap, 'sa-1');
      // main's touches (file 0, file 2) must not appear; only file 1, which
      // sa-1 itself created.
      expect(fileCoverage(lens)).toEqual({ edited: 1, read: 0, seen: 0 });
    }
  );
});

describe('recomputeTotals', () => {
  it('computes calls/requests/errors/PRs/file-ops from a bare event list', () => {
    const events = sessionMapSchema.parse({
      schema: SESSION_MAP_SCHEMA_V1,
      session: { id: 'x', startedAt: '2026-01-01T00:00:00Z' },
      totals: {},
      files: [{ id: 0, repo: 'r0', path: 'a.py' }],
      events: [
        { seq: 0, t: 0, kind: 'user', agent: 'main', err: false, chars: 5 },
        {
          seq: 1,
          t: 100,
          kind: 'llm',
          agent: 'main',
          err: false,
          model: 'claude-opus-5',
          in: 10,
          out: 5,
          cost: 0.1,
          latency: 500,
        },
        {
          seq: 2,
          t: 200,
          kind: 'edit',
          agent: 'main',
          err: false,
          targets: [{ file: 0, touch: 'edit', op: 'create' }],
        },
        {
          seq: 3,
          t: 300,
          kind: 'error',
          agent: 'main',
          err: true,
          source: 'tool',
          errClass: 'timeout',
        },
        {
          seq: 4,
          t: 400,
          kind: 'exec',
          agent: 'main',
          err: false,
          tool: 'gh',
          vcs: { provider: 'github', action: 'pr_create', number: 1 },
        },
      ],
    }).events;
    const totals = recomputeTotals(events);
    expect(totals.calls).toBe(2); // edit + exec, not llm/user/error
    expect(totals.requests).toBe(1);
    expect(totals.errors).toBe(1);
    expect(totals.errorsBySource).toEqual({ tool: 1 });
    expect(totals.errorsByClass).toEqual({ timeout: 1 });
    expect(totals.prsCreated).toBe(1);
    expect(totals.filesCreated).toBe(1);
    expect(totals.filesTouched).toBe(1);
    expect(totals.subagents).toBe(0);
  });
});

describe('describeEvent for failed hooks', () => {
  const title = (e: Record<string, unknown>) => {
    const m = makeMap([{ ...e, kind: 'error', err: true, source: 'hook' }]);
    return describeEvent(m, m.events[0]).title;
  };

  it('names the hook and whether it blocked', () => {
    expect(title({ errClass: 'hook_error', hook: 'PostToolUse:Bash' })).toBe('PostToolUse:Bash hook error');
    expect(title({ errClass: 'hook_blocking', hook: 'Stop' })).toBe('Stop hook blocked');
    expect(title({ errClass: 'hook_error' })).toBe('Hook error');
  });
});

describe('shiftStartedAt', () => {
  it('moves a valid timestamp forward as ISO', () => {
    expect(shiftStartedAt('2026-09-10T01:00:00Z', 6000)).toBe(
      '2026-09-10T01:00:06.000Z'
    );
  });

  it('returns an unparseable timestamp unchanged instead of throwing', () => {
    expect(shiftStartedAt('not a date', 6000)).toBe('not a date');
    expect(shiftStartedAt('', 0)).toBe('');
  });
});
