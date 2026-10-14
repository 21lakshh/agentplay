/**
 * Scoping: tasks, per-event model, and the scoped
 * document every view renders from. Pure, no React: tested in
 * `sessionScope.test.ts`.
 *
 * Scoping works the same way the agent lens does (`deriveLensDoc`): the
 * explorer is re-rendered from a filtered, reindexed copy of the map, so the
 * radial map, the timeline and every side card agree on what is in scope
 * without each one learning about filters.
 */
import { parseAsInteger, parseAsString } from 'nuqs';
import type {
  SessionMap,
  SessionMapAgent,
  SessionMapEvent,
} from './schema';
import {
  NON_CALL,
  ctxOf,
  outputTotals,
  recomputeTotals,
  shiftStartedAt,
  type OutputTotals,
} from './sessionMapModel';

// --- tasks -------------------------------------------------------------------

export interface TaskWindow {
  n: number;
  name: string;
  /** whether `name` came from the map (a segment or a prompt title) rather
   * than the "Task 3" / "Prompt 3" placeholder */
  named: boolean;
  /** `segments`: the CLI's own task segmentation; `turns`: one task per user
   * turn, derived here when the map carries no tasks */
  source: 'segments' | 'turns';
  /** first and last event index (inclusive) */
  start: number;
  end: number;
  /** ms since session start */
  startT: number;
  endT: number;
}

/** What the UI calls these windows. Locally every window is one user prompt
 * (`turns`); only a map with a real segmenter carries `segments` windows,
 * which are genuine tasks. */
export function taskNoun(tasks: readonly Pick<TaskWindow, 'source'>[]): {
  one: string;
  many: string;
} {
  return tasks.some((t) => t.source === 'segments')
    ? { one: 'Task', many: 'Tasks' }
    : { one: 'Prompt', many: 'Prompts' };
}

/**
 * Task number per event, and the task windows. Preference order:
 * 1. every event's own `task` (the CLI stamps it from `tasks[]`);
 * 2. `map.tasks[]` start/end sequence numbers (seq === index);
 * 3. user-turn windows: a task starts at each user turn and runs until the
 *    next one; events before the first turn belong to task 1.
 * A map with no user turns at all is one task. Never throws on a malformed
 * tasks block: it falls through to the next source.
 */
export function sessionTasks(map: SessionMap): {
  taskOf: number[];
  tasks: TaskWindow[];
} {
  const events = map.events;
  const N = events.length;
  const named = new Map<number, string>();
  const turnsTask = new Set<number>();
  for (const t of map.tasks ?? []) {
    if (typeof t.n !== 'number') continue;
    if (t.name) named.set(t.n, t.name);
    if (t.source === 'turns') turnsTask.add(t.n);
  }
  let taskOf: number[] | null = null;
  let source: TaskWindow['source'] = 'segments';

  if (N && events.every((e) => typeof e.task === 'number' && e.task > 0)) {
    taskOf = events.map((e) => e.task as number);
  } else if (map.tasks?.length) {
    const arr = new Array<number>(N).fill(0);
    for (const t of map.tasks) {
      if (t.startSeq == null || t.endSeq == null) continue;
      for (let i = Math.max(0, t.startSeq); i <= Math.min(N - 1, t.endSeq); i++)
        arr[i] = t.n;
    }
    // carry forward so events between declared windows join the prior task
    let last = 0;
    for (let i = 0; i < N; i++) {
      if (arr[i]) last = arr[i];
      else arr[i] = last;
    }
    const first = arr.find((v) => v > 0) ?? 0;
    if (first) {
      for (let i = 0; i < N && !arr[i]; i++) arr[i] = first;
      taskOf = arr;
    }
  }
  if (!taskOf) {
    source = 'turns';
    let k = 0;
    taskOf = events.map((e) => {
      if (e.kind === 'user') k++;
      return Math.max(1, k);
    });
  }

  const tasks: TaskWindow[] = [];
  taskOf.forEach((n, i) => {
    const last = tasks[tasks.length - 1];
    if (last && last.n === n) {
      last.end = i;
      last.endT = events[i].t;
    } else {
      const src = turnsTask.has(n) ? 'turns' : source;
      tasks.push({
        n,
        name: named.get(n) ?? (src === 'turns' ? `Prompt ${n}` : `Task ${n}`),
        named: named.has(n),
        source: src,
        start: i,
        end: i,
        startT: events[i].t,
        endT: events[i].t,
      });
    }
  });
  return { taskOf, tasks };
}

/**
 * Stamp every event with its task (and give the map a `tasks` block) so the
 * task survives later reindexing by an agent lens or a scope. A no-op when
 * the CLI already stamped every event.
 */
/**
 * Drop any `srcSeq` a document arrived with. It is the viewer's own field,
 * stamped by `deriveScopeDoc` / `deriveLensDoc` (see `srcSeqOf`), and rides the
 * schema's passthrough, so a map loaded from disk could carry one: trusted, it
 * would point a `?at=` link or a task's error tag at some other event. Applied
 * once, where the explorer takes in a raw map.
 */
export function withoutSrcSeq(map: SessionMap): SessionMap {
  if (!map.events.some((e) => e.srcSeq !== undefined)) return map;
  return {
    ...map,
    events: map.events.map((e) => {
      if (e.srcSeq === undefined) return e;
      const { srcSeq: _drop, ...rest } = e;
      return rest as SessionMapEvent;
    }),
  };
}

export function stampTasks(map: SessionMap): SessionMap {
  if (map.events.every((e) => typeof e.task === 'number' && e.task > 0))
    return map;
  const { taskOf, tasks } = sessionTasks(map);
  return {
    ...map,
    // Only a real name is carried: a "Prompt 3" placeholder written back here
    // would read as a title (`named`) on the next `sessionTasks` pass.
    tasks: tasks.map((t) => ({ n: t.n, name: t.named ? t.name : null, source: t.source })),
    events: map.events.map((e, i) => ({ ...e, task: taskOf[i] })),
  };
}

/** Summary of one task window for the side card. */
export interface TaskSummary {
  requests: number;
  calls: number;
  cost: number;
  tokens: number;
  outputs: OutputTotals;
  prsCreated: number;
  promptChars: number;
  promptImages: number;
  promptUrls: number;
  agents: string[];
  models: string[];
}

export function summarizeEvents(
  events: SessionMapEvent[],
  models: string[]
): TaskSummary {
  const llm = events.filter((e) => e.kind === 'llm');
  const users = events.filter((e) => e.kind === 'user');
  return {
    requests: llm.length,
    calls: events.filter((e) => !NON_CALL.has(e.kind)).length,
    cost: llm.reduce((s, e) => s + (e.cost ?? 0), 0),
    tokens: llm.reduce((s, e) => s + ctxOf(e) + (e.out ?? 0), 0),
    outputs: outputTotals(events),
    prsCreated: events.filter((e) => e.vcs?.action === 'pr_create').length,
    promptChars: users.reduce((s, e) => s + (e.chars ?? 0), 0),
    promptImages: users.reduce((s, e) => s + (e.images ?? 0), 0),
    promptUrls: users.reduce((s, e) => s + (e.urls ?? 0), 0),
    agents: [...new Set(events.map((e) => e.agent))],
    models: [...new Set(models.filter(Boolean))],
  };
}

// --- models ------------------------------------------------------------------

/**
 * The model that drove each event: the event's own `model` (newer CLIs stamp
 * every event), else the most recent LLM request's model on the same agent
 * (carried forward, then backward to cover events before the first request),
 * else the session's first model. '' when the map names no model at all.
 */
export function eventModels(map: SessionMap): string[] {
  const events = map.events;
  const out = new Array<string>(events.length).fill('');
  const lastByAgent = new Map<string, string>();
  events.forEach((e, i) => {
    const own = e.kind === 'model_switch' ? e.to : e.model;
    if (own) lastByAgent.set(e.agent, own);
    out[i] = own || lastByAgent.get(e.agent) || '';
  });
  const nextByAgent = new Map<string, string>();
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (out[i]) nextByAgent.set(e.agent, out[i]);
    else out[i] = nextByAgent.get(e.agent) || map.session.models[0] || '';
  }
  return out;
}

export interface ModelUsage {
  model: string;
  requests: number;
  cost: number;
  tokens: number;
}

/** Per-model request/cost/token rollup, most expensive first. */
export function modelUsage(map: SessionMap): ModelUsage[] {
  const by = new Map<string, ModelUsage>();
  // Same attribution as the model filter: a request with no
  // model of its own must land in the bucket the filter would put it in.
  const resolved = eventModels(map);
  for (const [i, e] of map.events.entries()) {
    if (e.kind !== 'llm') continue;
    const m = resolved[i];
    if (!m) continue;
    const u = by.get(m) ?? { model: m, requests: 0, cost: 0, tokens: 0 };
    u.requests++;
    u.cost += e.cost ?? 0;
    u.tokens += ctxOf(e) + (e.out ?? 0);
    by.set(m, u);
  }
  return [...by.values()].sort(
    (a, b) => b.cost - a.cost || b.requests - a.requests
  );
}

// --- scope -------------------------------------------------------------------

export interface Scope {
  /** task number, or null for the whole session */
  task: number | null;
  /** model name, or null for every model */
  model: string | null;
}

export const EMPTY_SCOPE: Scope = { task: null, model: null };

/**
 * URL params for the scope (`?task=2&model=sonnet-5`): filters live in the
 * URL so reload, share and Back restore them. Shared by the explorer and the
 * session list (which clears them on a session switch, since task numbers
 * and models belong to one session).
 */
export const SCOPE_PARAMS = {
  task: parseAsInteger,
  model: parseAsString,
  /** an open subagent (agent id): the replay is lensed onto that helper's
   * events (`?sub=<agentId>`), so a subagent view is linkable */
  sub: parseAsString,
  /** the session the scope was set on: a scope never carries over to
   * another session, however the selection changed (click, search, window) */
  ss: parseAsString,
};

export const CLEAR_SCOPE_PARAMS = {
  task: null,
  model: null,
  sub: null,
  ss: null,
};

/**
 * The scope that applies to `sessionId`, from the URL params. A scope set on
 * another session reads as empty. A scope with no owner (a hand-written
 * link) is taken to belong to the session it is first shown with.
 */
export function scopeForSession(
  p: {
    task: number | null;
    model: string | null;
    sub?: string | null;
    ss: string | null;
  },
  sessionId: string
): { scope: Scope; sub: string | null; stale: boolean; unowned: boolean } {
  const sub = p.sub ?? null;
  const any = p.task != null || p.model != null || sub != null;
  if (p.ss != null && p.ss !== sessionId)
    return { scope: EMPTY_SCOPE, sub: null, stale: any, unowned: false };
  return {
    scope: { task: p.task, model: p.model },
    sub,
    stale: false,
    // the caller stamps it with this session so it cannot carry over later
    unowned: any && p.ss == null,
  };
}

/**
 * Keep only the repos and files the kept events touch, so a scoped map does
 * not draw unrelated files as hollow nodes. Files keep their ids (event
 * targets point at them), repos stay those of the kept files.
 */
export function scopeFiles(
  map: SessionMap,
  events: SessionMapEvent[]
): Pick<SessionMap, 'repos' | 'files'> {
  const fileIds = new Set<number>();
  for (const e of events)
    for (const t of e.targets ?? [])
      if (!t.scope && t.file != null) fileIds.add(t.file);
  const files = map.files.filter((f) => fileIds.has(f.id));
  const repoIds = new Set(files.map((f) => f.repo));
  return { repos: map.repos.filter((r) => repoIds.has(r.id)), files };
}

export const isScoped = (s: Scope): boolean =>
  s.task != null || s.model != null;

/**
 * Scope a map (the full map, before any agent lens) to a task window, a
 * model, or both. Returns `map` itself when nothing is scoped.
 *
 * Kept events are reindexed; `retryOf`/`ref` are remapped to the new
 * positions or dropped when their target fell out of scope (a retry arc to
 * an event that is not drawn would point at the wrong mark). Times are
 * rebased to the first kept event so the timeline spans the scope, and
 * `session.startedAt` moves with them so clock labels stay true. Each event
 * keeps its `task` and gets its resolved `model`, so the task lane and model
 * branch still read correctly inside a scope.
 */
export function deriveScopeDoc(map: SessionMap, scope: Scope): SessionMap {
  if (!isScoped(scope) || !map.events.length) return map;
  const { taskOf } = sessionTasks(map);
  const models = eventModels(map);
  const keep: number[] = [];
  map.events.forEach((e, i) => {
    if (scope.task != null && taskOf[i] !== scope.task) return;
    if (scope.model != null && e.kind !== 'user' && models[i] !== scope.model)
      return;
    keep.push(i);
  });
  if (!keep.length)
    // Nothing matches: zero totals and duration, never the full session's.
    return {
      ...map,
      session: { ...map.session, durationMs: 0 },
      totals: recomputeTotals([]),
      repos: [],
      files: [],
      events: [],
      agents: [],
      prs: [],
    };

  const oldToNew = new Map<number, number>();
  keep.forEach((oldIndex, i) => oldToNew.set(oldIndex, i));
  const t0 = map.events[keep[0]].t;
  const events = keep.map((oldIndex, i) => {
    const e = map.events[oldIndex];
    const ev: SessionMapEvent = {
      ...e,
      seq: i,
      // `seq` is renumbered to stay `seq === index` here; `srcSeq` keeps the
      // full document's number so a reference held across the derivation
      // still resolves (`srcSeqOf`). Set once, by the first derivation.
      srcSeq: typeof e.srcSeq === 'number' ? e.srcSeq : (e.seq ?? oldIndex),
      t: e.t - t0,
      task: taskOf[oldIndex],
      model: e.kind === 'model_switch' ? e.model : models[oldIndex] || e.model,
    };
    if (ev.retryOf != null) ev.retryOf = oldToNew.get(ev.retryOf);
    if (ev.ref != null) ev.ref = oldToNew.get(ev.ref);
    return ev;
  });
  const durationMs = events[events.length - 1].t;
  const kept = new Set(keep);

  const agents: SessionMapAgent[] = map.agents
    .map((a) => {
      const own = events.filter((e) => e.agent === a.id);
      if (!own.length) return null;
      const llm = own.filter((e) => e.kind === 'llm');
      return {
        ...a,
        start: own[0].t,
        end: own[own.length - 1].t,
        calls: own.filter((e) => !NON_CALL.has(e.kind)).length,
        requests: llm.length,
        tokens: llm.reduce((s, e) => s + ctxOf(e) + (e.out ?? 0), 0),
        cost: llm.reduce((s, e) => s + (e.cost ?? 0), 0),
      };
    })
    .filter((a): a is SessionMapAgent => a != null);

  const totals = recomputeTotals(events);
  return {
    ...map,
    ...scopeFiles(map, events),
    session: {
      ...map.session,
      startedAt: shiftStartedAt(map.session.startedAt, t0),
      durationMs,
    },
    totals: {
      ...totals,
      subagents: agents.filter((a) => a.id !== 'main').length,
    },
    prs: map.prs
      .filter((pr) => kept.has(pr.seq))
      .map((pr) => ({ ...pr, seq: oldToNew.get(pr.seq) ?? pr.seq })),
    agents,
    events,
  };
}

/**
 * Whether a (scoped) map has anything to replay. A model filter keeps every
 * `user` event, so a prompt that used none of the model survives as its bare
 * prompt node: that is an empty scope, not a replay.
 */
export const hasReplayEvents = (map: SessionMap): boolean =>
  map.events.some((e) => e.kind !== 'user');

/**
 * The model picker's rows as counted inside a task scope (`map` is the
 * task-scoped doc, no model filter): every model stays listed, with zero
 * requests where the prompt did not use it.
 */
export function scopedModelUsage(
  models: readonly ModelUsage[],
  map: SessionMap
): ModelUsage[] {
  const inScope = new Map(modelUsage(map).map((u) => [u.model, u]));
  return models.map(
    (m) => inScope.get(m.model) ?? { ...m, requests: 0, cost: 0, tokens: 0 }
  );
}

/**
 * A scope from the URL, with a task this session does not have or a model it
 * never used read as unset, so a stale link cannot leave a filter looking
 * active over an empty replay.
 */
export function normalizeScope(
  scope: Scope,
  tasks: readonly Pick<TaskWindow, 'n'>[],
  models: readonly Pick<ModelUsage, 'model'>[]
): Scope {
  const task =
    scope.task != null && tasks.some((t) => t.n === scope.task)
      ? scope.task
      : null;
  const model =
    scope.model != null && models.some((m) => m.model === scope.model)
      ? scope.model
      : null;
  return task === scope.task && model === scope.model
    ? scope
    : { task, model };
}

// --- timeline lanes -----------------------------------------------------------

/** Timeline lanes, in drawing order. Every point event draws on the one
 * `events` bar (the dock's category chips pick what is bright); Context and
 * Subagents are optional lanes switched by their own dock toggles. */
export type LaneKey = 'tasks' | 'events' | 'ctx' | 'main' | 'subs';

export const LANE_LABEL: Record<LaneKey, string> = {
  tasks: 'Tasks',
  events: 'Events',
  ctx: 'Context',
  main: 'Main agent',
  subs: 'Subagents',
};

export const LANE_KEYS = Object.keys(LANE_LABEL) as LaneKey[];

/** The lanes to draw: tasks, the Events bar and the main agent always; the
 * Context and Subagents lanes when their toggles are on. */
export function timelineLanes(opts: {
  context: boolean;
  subagents: boolean;
}): Set<LaneKey> {
  return new Set(
    LANE_KEYS.filter(
      (k) => (k !== 'ctx' || opts.context) && (k !== 'subs' || opts.subagents)
    )
  );
}
