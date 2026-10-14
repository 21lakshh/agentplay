import { describe, expect, it } from 'vitest';
import {
  SESSION_MAP_SCHEMA_V1,
  sessionMapSchema,
  type SessionMap,
} from './schema';
import {
  buildTree,
  deriveLensDoc,
  describeEvent,
  eventNodeRefs,
  findNext,
  outputTotals,
  recomputeTotals,
} from './sessionMapModel';
import {
  EMPTY_SCOPE,
  LANE_KEYS,
  deriveScopeDoc,
  eventModels,
  hasReplayEvents,
  modelUsage,
  normalizeScope,
  scopeForSession,
  scopedModelUsage,
  sessionTasks,
  stampTasks,
  summarizeEvents,
  taskNoun,
  timelineLanes,
} from './sessionScope';

type RawEvent = Record<string, unknown> & { kind: string };

function makeMap(
  events: RawEvent[],
  overrides: Record<string, unknown> = {}
): SessionMap {
  return sessionMapSchema.parse({
    schema: SESSION_MAP_SCHEMA_V1,
    session: {
      id: 'local:claude-code:s1',
      agentTool: 'claude-code',
      startedAt: '2026-09-23T15:44:00Z',
      durationMs: 1_080_000,
      models: ['opus-5.5'],
    },
    totals: { calls: 0, requests: 0 },
    repos: [{ id: 'r0', name: 'dashboard' }],
    files: [{ id: 0, repo: 'r0', path: 'src/proxy.ts' }],
    agents: [
      { id: 'main', parent: null, kind: 'main' },
      { id: 'sub1', parent: 'main', kind: 'subagent', subagentType: 'Explore' },
    ],
    prs: [],
    events: events.map((e, i) => ({
      seq: i,
      t: i * 1000,
      agent: 'main',
      ...e,
    })),
    ...overrides,
  });
}

/** A 3-task CSP-fix session: diagnose, fix + open PR, verify + merge. */
const CSP_EVENTS: RawEvent[] = [
  { kind: 'user', chars: 1240, urls: 1 }, // 0  task 1
  { kind: 'llm', model: 'opus-5.5', cost: 0.05, in: 100, out: 20 }, // 1
  { kind: 'read', targets: [{ file: 0, touch: 'read' }] }, // 2
  { kind: 'subagent', child: 'sub1', subagentType: 'Explore' }, // 3
  { kind: 'llm', agent: 'sub1', model: 'haiku-4.5', cost: 0.01 }, // 4
  { kind: 'search', agent: 'sub1' }, // 5
  { kind: 'user', chars: 310, images: 1 }, // 6  task 2
  { kind: 'llm', model: 'opus-5.5', cost: 0.1 }, // 7
  { kind: 'edit', targets: [{ file: 0, touch: 'edit' }] }, // 8
  { kind: 'exec', err: true, errClass: 'exit_code' }, // 9
  { kind: 'exec', retryOf: 9 }, // 10
  {
    kind: 'commit',
    repo: 'dashboard',
    sha: 'a1b2c3d4',
    files: 1,
    ins: 2,
    del: 0,
  }, // 11
  {
    kind: 'exec',
    vcs: {
      provider: 'github',
      action: 'pr_create',
      repo: 'dashboard',
      number: 42,
    },
  }, // 12
  { kind: 'pr_comment', repo: 'dashboard', number: 42 }, // 13
  { kind: 'user', chars: 95 }, // 14 task 3
  { kind: 'model_switch', from: 'opus-5.5', to: 'sonnet-5' }, // 15
  { kind: 'llm', model: 'sonnet-5', cost: 0.02 }, // 16
  { kind: 'pr_merge', repo: 'dashboard', number: 42 }, // 17
  { kind: 'ticket', tracker: 'linear', action: 'update', id: 'ACME-4767' }, // 18
  { kind: 'artifact', title: 'proposal', bytes: 840_000, version: 3 }, // 19
  { kind: 'skill_write', name: 'session-map-directive', action: 'create' }, // 20
];

describe('sessionTasks', () => {
  it('falls back to one task per user turn when the map has no tasks', () => {
    const { taskOf, tasks } = sessionTasks(makeMap(CSP_EVENTS));
    expect(tasks.map((t) => [t.n, t.start, t.end, t.source])).toEqual([
      [1, 0, 5, 'turns'],
      [2, 6, 13, 'turns'],
      [3, 14, 20, 'turns'],
    ]);
    expect(tasks[0].name).toBe('Prompt 1');
    expect(taskOf[9]).toBe(2);
  });

  it('marks a turn task named only when the map titled it', () => {
    const map = makeMap(CSP_EVENTS, {
      tasks: [{ n: 2, name: 'fix the flaky test', source: 'turns' }],
    });
    const { tasks } = sessionTasks(map);
    expect(tasks.map((t) => [t.n, t.name, t.named])).toEqual([
      [1, 'Prompt 1', false],
      [2, 'fix the flaky test', true],
      [3, 'Prompt 3', false],
    ]);
  });

  it('events before the first user turn join task 1; no turns = one task', () => {
    const { taskOf } = sessionTasks(
      makeMap([{ kind: 'read' }, { kind: 'user' }, { kind: 'read' }])
    );
    expect(taskOf).toEqual([1, 1, 1]);
    const one = sessionTasks(makeMap([{ kind: 'read' }, { kind: 'llm' }]));
    expect(one.tasks).toHaveLength(1);
  });

  it('prefers the CLI segmentation (tasks[] start/end seq) and its names', () => {
    const map = makeMap(CSP_EVENTS, {
      tasks: [
        { n: 1, name: 'Diagnose', source: 'segments', startSeq: 0, endSeq: 8 },
        { n: 2, name: 'Ship', source: 'segments', startSeq: 11, endSeq: 20 },
      ],
    });
    const { taskOf, tasks } = sessionTasks(map);
    expect(tasks.map((t) => t.name)).toEqual(['Diagnose', 'Ship']);
    // 9 and 10 fall between windows: carried forward into task 1
    expect(taskOf.slice(8, 12)).toEqual([1, 1, 1, 2]);
    expect(tasks[0].source).toBe('segments');
  });

  it('prefers per-event task stamps over everything else', () => {
    const map = makeMap(
      CSP_EVENTS.map((e, i) => ({ ...e, task: i < 10 ? 1 : 2 })),
      {
        tasks: [
          { n: 1, name: 'A' },
          { n: 2, name: 'B' },
        ],
      }
    );
    const { tasks } = sessionTasks(map);
    expect(tasks.map((t) => [t.n, t.name, t.start, t.end])).toEqual([
      [1, 'A', 0, 9],
      [2, 'B', 10, 20],
    ]);
  });

  it('survives a malformed tasks block (schema drops it)', () => {
    const map = makeMap(CSP_EVENTS, { tasks: 'garbage' });
    expect(map.tasks).toBeUndefined();
    expect(sessionTasks(map).tasks).toHaveLength(3);
  });

  it('stampTasks keeps turn-derived names through reindexing', () => {
    const stamped = stampTasks(makeMap(CSP_EVENTS));
    expect(stamped.events.every((e) => typeof e.task === 'number')).toBe(true);
    const lens = deriveLensDoc(stamped, 'sub1');
    const { tasks } = sessionTasks(lens);
    expect(tasks).toHaveLength(1);
    expect(tasks[0].name).toBe('Prompt 1');
    // the placeholder is regenerated, never persisted as if it were a title
    expect(tasks[0].named).toBe(false);
    expect(tasks[0].source).toBe('turns');
    // already stamped: returned as-is
    expect(stampTasks(stamped)).toBe(stamped);
  });
});

describe('taskNoun', () => {
  it('calls user-turn windows prompts', () => {
    expect(taskNoun([{ source: 'turns' }, { source: 'turns' }])).toEqual({
      one: 'Prompt',
      many: 'Prompts',
    });
    expect(taskNoun([])).toEqual({ one: 'Prompt', many: 'Prompts' });
  });

  it('keeps tasks when any window comes from a segmenter', () => {
    expect(taskNoun([{ source: 'turns' }, { source: 'segments' }])).toEqual({
      one: 'Task',
      many: 'Tasks',
    });
  });
});

describe('models', () => {
  it('attributes every event to the model driving its agent', () => {
    const models = eventModels(makeMap(CSP_EVENTS));
    expect(models[0]).toBe('opus-5.5'); // before the first request: back-filled
    expect(models[2]).toBe('opus-5.5');
    expect(models[5]).toBe('haiku-4.5'); // subagent keeps its own model
    expect(models[15]).toBe('sonnet-5'); // model switch
    expect(models[17]).toBe('sonnet-5'); // after the switch
  });

  it('falls back to the session model when no request names one', () => {
    const models = eventModels(makeMap([{ kind: 'read' }, { kind: 'llm' }]));
    expect(models).toEqual(['opus-5.5', 'opus-5.5']);
  });

  it('modelUsage rolls up requests and cost, most expensive first', () => {
    const u = modelUsage(makeMap(CSP_EVENTS));
    expect(u.map((m) => [m.model, m.requests])).toEqual([
      ['opus-5.5', 2],
      ['sonnet-5', 1],
      ['haiku-4.5', 1],
    ]);
    expect(u[0].cost).toBeCloseTo(0.15);
  });
});

describe('deriveScopeDoc', () => {
  const map = makeMap(CSP_EVENTS);

  it('returns the map itself when nothing is scoped', () => {
    expect(deriveScopeDoc(map, EMPTY_SCOPE)).toBe(map);
  });

  it('scopes to a task window, rebased, with retries remapped', () => {
    const d = deriveScopeDoc(map, { ...EMPTY_SCOPE, task: 2 });
    expect(d.events).toHaveLength(8);
    expect(d.events[0].kind).toBe('user');
    expect(d.events[0].t).toBe(0);
    expect(d.events.every((e, i) => e.seq === i)).toBe(true);
    expect(d.events.every((e) => e.task === 2)).toBe(true);
    // exec retry (old 10 -> old 9) is now 4 -> 3
    expect(d.events[4].retryOf).toBe(3);
    expect(d.session.startedAt).toBe('2026-09-23T15:44:06.000Z');
    expect(d.totals.commits).toBe(1);
    expect(d.totals.prComments).toBe(1);
    expect(d.totals.requests).toBe(1);
  });

  it('keeps an unparseable startedAt instead of throwing', () => {
    const bad = makeMap(
      [{ kind: 'user' }, { kind: 'user', t: 5000 }],
      { session: { ...makeMap([{ kind: 'user' }]).session, startedAt: 'garbage' } }
    );
    const d = deriveScopeDoc(bad, { ...EMPTY_SCOPE, task: 2 });
    expect(d.session.startedAt).toBe('garbage');
  });

  it('drops a retry link whose failed attempt fell out of scope', () => {
    const onlyRetry = deriveScopeDoc(
      makeMap([
        { kind: 'user' },
        { kind: 'exec', err: true },
        { kind: 'user' },
        { kind: 'read', retryOf: 1 },
      ]),
      { ...EMPTY_SCOPE, task: 2 }
    );
    expect(onlyRetry.events.map((e) => e.kind)).toEqual(['user', 'read']);
    expect(onlyRetry.events[1].retryOf).toBeUndefined();
  });

  it('scopes to one model and keeps the task structure', () => {
    const d = deriveScopeDoc(map, { ...EMPTY_SCOPE, model: 'sonnet-5' });
    // user turns stay, sonnet-driven events from the switch on
    expect(d.events.map((e) => e.kind)).toEqual([
      'user',
      'user',
      'user',
      'model_switch',
      'llm',
      'pr_merge',
      'ticket',
      'artifact',
      'skill_write',
    ]);
    expect(d.events[0].task).toBe(1);
    expect(d.agents.map((a) => a.id)).toEqual(['main']);
    expect(d.totals.subagents).toBe(0);
  });

  it('recomputes per-agent rollups inside the scope', () => {
    const d = deriveScopeDoc(map, { ...EMPTY_SCOPE, task: 1 });
    const sub = d.agents.find((a) => a.id === 'sub1');
    expect(sub?.requests).toBe(1);
    expect(sub?.cost).toBeCloseTo(0.01);
    expect(d.totals.subagents).toBe(1);
  });

  it('keeps and reindexes PRs whose creating event is in scope', () => {
    const withPr = makeMap(CSP_EVENTS, {
      prs: [
        {
          provider: 'github',
          repo: 'dashboard',
          number: 42,
          url: 'u',
          seq: 12,
        },
      ],
    });
    expect(deriveScopeDoc(withPr, { ...EMPTY_SCOPE, task: 2 }).prs[0].seq).toBe(
      6
    );
    expect(deriveScopeDoc(withPr, { ...EMPTY_SCOPE, task: 3 }).prs).toEqual([]);
  });

  it('an empty scope yields an empty document, not the whole session', () => {
    const d = deriveScopeDoc(map, { ...EMPTY_SCOPE, model: 'no-such-model' });
    // only user turns match a model filter they are exempt from
    expect(d.events.every((e) => e.kind === 'user')).toBe(true);
    const none = deriveScopeDoc(makeMap([{ kind: 'read' }]), {
      ...EMPTY_SCOPE,
      model: 'no-such-model',
    });
    expect(none.events).toEqual([]);
  });
});

describe('outputs on the map, jumps and descriptions', () => {
  const map = makeMap(CSP_EVENTS);

  it('builds a models branch and an outputs branch with one node per PR', () => {
    const tree = buildTree(map);
    const models = tree.byId.get('g:models');
    expect(models?.children.map((c) => c.name).sort()).toEqual([
      'haiku-4.5',
      'opus-5.5',
      'sonnet-5',
    ]);
    const outs = tree.byId.get('g:outputs');
    expect(outs?.children.map((c) => c.id).sort()).toEqual([
      'outg:artifacts',
      'outg:github',
      'outg:skills',
      'outg:tickets',
    ]);
    const gh = tree.byId.get('outg:github');
    // commit + a single PR node for create, comment and merge
    expect(gh?.children.map((c) => c.id).sort()).toEqual([
      'commit:a1b2c3d4',
      'pr:dashboard#42',
    ]);
    expect(tree.byId.get('pr:dashboard#42')?.type).toBe('output');
  });

  it('lights model nodes from requests and output nodes from outputs', () => {
    const tree = buildTree(map);
    const refs = eventNodeRefs(map, tree);
    expect(refs[1].map((r) => r.id)).toContain('model:opus-5.5');
    expect(refs[15].map((r) => r.id)).toContain('model:sonnet-5');
    expect(refs[17].map((r) => r.id)).toContain('pr:dashboard#42');
    expect(refs[12].map((r) => r.id)).toContain('pr:dashboard#42');
  });

  it('jumps to the next output, wrapping', () => {
    expect(findNext(map.events, 0, 'out')).toBe(11);
    expect(findNext(map.events, 11, 'out')).toBe(12);
    expect(findNext(map.events, 20, 'out')).toBe(11);
  });

  it('outputs are not tool calls and are counted per kind', () => {
    const t = recomputeTotals(map.events);
    expect(t.calls).toBe(7); // read, subagent, search, edit, 3x exec
    expect(outputTotals(map.events)).toEqual({
      commits: 1,
      prsMerged: 1,
      prsClosed: 0,
      prComments: 1,
      ticketsCreated: 0,
      ticketsUpdated: 1,
      artifacts: 1,
      skillsCreated: 1,
      skillsUpdated: 0,
      modelSwitches: 1,
    });
  });

  it('describes the new kinds without content', () => {
    const d = (i: number) => describeEvent(map, map.events[i]);
    expect(d(0).detail).toBe('1240 chars · 1 URL (text not stored)');
    expect(d(11).title).toBe('Commit a1b2c3d');
    expect(d(11).detail).toContain('+2 −0');
    expect(d(17).title).toBe('PR #42 merged');
    expect(d(18).title).toBe('Ticket updated · ACME-4767');
    expect(d(19).detail).toBe('840.0 KB · version 3');
    expect(d(15).title).toBe('Model switched to sonnet-5');
  });

  it('summarizes a task window', () => {
    const d = deriveScopeDoc(map, { ...EMPTY_SCOPE, task: 2 });
    const s = summarizeEvents(
      d.events,
      d.events.map((e) => e.model ?? '')
    );
    expect(s.promptChars).toBe(310);
    expect(s.promptImages).toBe(1);
    expect(s.prsCreated).toBe(1);
    expect(s.outputs.commits).toBe(1);
    expect(s.models).toEqual(['opus-5.5']);
  });
});

describe('lanes', () => {
  it('draws tasks, the Events bar and the main agent; Context and Subagents on their toggles', () => {
    expect([...timelineLanes({ context: false, subagents: false })]).toEqual([
      'tasks',
      'events',
      'main',
    ]);
    expect([...timelineLanes({ context: true, subagents: true })]).toEqual(
      LANE_KEYS
    );
    expect([...timelineLanes({ context: true, subagents: false })]).toEqual([
      'tasks',
      'events',
      'ctx',
      'main',
    ]);
  });
});

describe('scope totals', () => {
  it('a scope with no matching events has zero totals, not the session ones', () => {
    const d = deriveScopeDoc(
      makeMap([{ kind: 'read' }, { kind: 'llm', cost: 1 }]),
      {
        ...EMPTY_SCOPE,
        model: 'no-such-model',
      }
    );
    expect(d.events).toEqual([]);
    expect(d.totals.requests).toBe(0);
    expect(d.totals.costUsd).toBe(0);
    expect(d.session.durationMs).toBe(0);
  });

  it('modelUsage buckets match the model filter attribution', () => {
    // a request with no model of its own, after a switch on the same agent
    const map = makeMap(
      [
        { kind: 'llm', model: 'opus-5.5', cost: 1 },
        { kind: 'model_switch', from: 'opus-5.5', to: 'sonnet-5' },
        { kind: 'llm', cost: 2 },
      ],
      { session: { id: 's', startedAt: '2026-09-23T15:44:00Z', models: [] } }
    );
    const usage = modelUsage(map);
    expect(usage.map((u) => [u.model, u.requests])).toEqual([
      ['sonnet-5', 1],
      ['opus-5.5', 1],
    ]);
    const scoped = deriveScopeDoc(map, { ...EMPTY_SCOPE, model: 'sonnet-5' });
    expect(scoped.events.filter((e) => e.kind === 'llm')).toHaveLength(1);
    // no model anywhere: no bucket, rather than an "unknown" chip that
    // would scope to nothing
    expect(
      modelUsage(
        makeMap([{ kind: 'llm' }], {
          session: { id: 's', startedAt: '2026-09-23T15:44:00Z', models: [] },
        })
      )
    ).toEqual([]);
  });

  it('task windows keep full-session offsets under a scope that drops the prefix', () => {
    const map = stampTasks(makeMap(CSP_EVENTS));
    const full = sessionTasks(map).tasks;
    // a task scope drops everything before task 3 (a model scope keeps the
    // user turns, so it would not): the scoped doc starts at task 3
    const scoped = deriveScopeDoc(map, { ...EMPTY_SCOPE, task: 3 });
    // compare instants: '…Z' and '….000Z' are the same time as strings differ
    expect(Date.parse(scoped.session.startedAt)).toBe(
      Date.parse(map.session.startedAt) + 14_000
    );
    // full-map windows are measured from the full session start: adding their
    // offset to the scoped start would put task 3 at +28s instead of +14s,
    // which is why TaskCard is given the full session start
    expect(full[2].startT).toBe(14_000);
    expect(Date.parse(map.session.startedAt) + full[2].startT).toBe(
      Date.parse(scoped.session.startedAt)
    );
  });
});

describe('scope binding and repos', () => {
  it('a scope set on one session does not apply to another', () => {
    const p = { task: 2, model: null, ss: 'A' };
    expect(scopeForSession(p, 'A')).toEqual({
      scope: { task: 2, model: null },
      sub: null,
      stale: false,
      unowned: false,
    });
    expect(scopeForSession(p, 'B')).toEqual({
      scope: EMPTY_SCOPE,
      sub: null,
      stale: true,
      unowned: false,
    });
    // unowned deep link: applies to the session it is shown with, flagged so
    // the caller binds it to that session
    const u = scopeForSession({ ...p, ss: null }, 'B');
    expect(u.scope.task).toBe(2);
    expect(u.unowned).toBe(true);
    expect(
      scopeForSession({ task: null, model: null, ss: null }, 'B').unowned
    ).toBe(false);
    // nothing scoped on another session: not stale, nothing to clear
    expect(
      scopeForSession({ task: null, model: null, ss: 'A' }, 'B').stale
    ).toBe(false);
  });

  it('an open subagent (?sub=) is bound to its session like the scope', () => {
    const p = { task: null, model: null, sub: 'sub1', ss: 'A' };
    expect(scopeForSession(p, 'A').sub).toBe('sub1');
    const other = scopeForSession(p, 'B');
    expect(other.sub).toBeNull();
    expect(other.stale).toBe(true);
    // a hand-written ?sub= link is claimed by the session it is shown with
    expect(scopeForSession({ ...p, ss: null }, 'B')).toMatchObject({
      sub: 'sub1',
      unowned: true,
    });
  });

  it('a scoped map keeps only the repos and files its events touch', () => {
    const map = makeMap(
      [
        { kind: 'user' },
        { kind: 'read', targets: [{ file: 0, touch: 'read' }] },
        { kind: 'user' },
        { kind: 'edit', targets: [{ file: 1, touch: 'edit' }] },
      ],
      {
        repos: [
          { id: 'r0', name: 'dashboard' },
          { id: 'r1', name: 'backend' },
        ],
        files: [
          { id: 0, repo: 'r0', path: 'src/proxy.ts' },
          { id: 1, repo: 'r1', path: 'views.py' },
        ],
      }
    );
    const d = deriveScopeDoc(map, { ...EMPTY_SCOPE, task: 2 });
    expect(d.files.map((f) => f.id)).toEqual([1]);
    expect(d.repos.map((r) => r.name)).toEqual(['backend']);
    const tree = buildTree(d);
    expect(tree.byId.has('file:0')).toBe(false);
    expect(tree.byId.has('repo:r0')).toBe(false);
    // unscoped: untouched
    expect(deriveScopeDoc(map, EMPTY_SCOPE).files).toHaveLength(2);
  });
});

describe('scope filter helpers', () => {
  // task 1 ran opus only, task 2 ran sonnet only
  const map = stampTasks(
    makeMap([
      { kind: 'user' },
      { kind: 'llm', model: 'opus-5.5', cost: 1 },
      { kind: 'user' },
      { kind: 'llm', model: 'sonnet-5', cost: 2 },
      { kind: 'llm', model: 'sonnet-5', cost: 3 },
    ])
  );
  const tasks = sessionTasks(map).tasks;
  const models = modelUsage(map);

  it('hasReplayEvents: a prompt with no event of the model is not a replay', () => {
    const miss = deriveScopeDoc(map, { task: 1, model: 'sonnet-5' });
    // the model filter keeps the prompt's own `user` event
    expect(miss.events.map((e) => e.kind)).toEqual(['user']);
    expect(hasReplayEvents(miss)).toBe(false);
    expect(hasReplayEvents(deriveScopeDoc(map, { task: 2, model: 'sonnet-5' }))).toBe(true);
    expect(hasReplayEvents(map)).toBe(true);
    expect(hasReplayEvents({ ...map, events: [] })).toBe(false);
  });

  it('scopedModelUsage: per-model requests inside one prompt, zero kept', () => {
    const inTask = (n: number) =>
      scopedModelUsage(models, deriveScopeDoc(map, { task: n, model: null }));
    const reqs = (u: { model: string; requests: number }[]) =>
      Object.fromEntries(u.map((m) => [m.model, m.requests]));
    expect(reqs(inTask(1))).toEqual({ 'opus-5.5': 1, 'sonnet-5': 0 });
    expect(reqs(inTask(2))).toEqual({ 'opus-5.5': 0, 'sonnet-5': 2 });
    expect(inTask(2).map((m) => m.model)).toEqual(models.map((m) => m.model));
    expect(reqs(scopedModelUsage(models, map))).toEqual({
      'opus-5.5': 1,
      'sonnet-5': 2,
    });
  });

  it('normalizeScope: an out-of-range task or unknown model reads as unset', () => {
    const n = (task: number | null, model: string | null) =>
      normalizeScope({ task, model }, tasks, models);
    expect(n(2, 'sonnet-5')).toEqual({ task: 2, model: 'sonnet-5' });
    expect(n(999, 'sonnet-5')).toEqual({ task: null, model: 'sonnet-5' });
    expect(n(0, null)).toEqual(EMPTY_SCOPE);
    expect(n(1, 'nope')).toEqual({ task: 1, model: null });
    expect(n(null, null)).toEqual(EMPTY_SCOPE);
  });
});
