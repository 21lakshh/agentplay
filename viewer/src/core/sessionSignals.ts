/**
 * Signals for the Session Explorer (errors, retries,
 * delays, context, LLM output): context series with compaction drops, the
 * idle-compressed time axis, retry pairs, per-playhead signal counts, session
 * signal totals and the per-kind detail rows the inspector and tooltips show.
 * Pure, unit-tested in `sessionSignals.test.ts`.
 */
import type { SessionMap, SessionMapEvent } from './schema';
import {
  DELAY_CAUSE_LABEL,
  ERROR_SOURCES,
  ERROR_SOURCE_LABEL,
  agentLabel,
  ctxOf,
  describeEvent,
  errClassLabel,
  errClassOf,
  errSource,
  fmtDur,
  fmtTok,
  fmtUsd,
  type ErrorSource,
} from './sessionMapModel';

// --- context ----------------------------------------------------------------

export interface ContextSeries {
  /** main-agent context after event i (carried forward between requests) */
  values: Float64Array;
  /** for a compaction at i: the context just before it dropped */
  before: Map<number, number>;
  /** y-scale max: totals.maxContext, any compaction's preTokens, or the peak */
  max: number;
}

/**
 * Main-agent context over the session: each main `llm` sets it to
 * in + cw + cr; a main compaction first climbs to `preTokens` (or the
 * carried value) then drops to `postTokens`.
 */
export function contextSeries(map: SessionMap): ContextSeries {
  const events = map.events;
  const values = new Float64Array(events.length);
  const before = new Map<number, number>();
  let cur = 0;
  let peak = 0;
  events.forEach((e, i) => {
    if (e.agent === 'main' && e.kind === 'llm') cur = ctxOf(e);
    if (e.kind === 'compaction' && e.agent === 'main') {
      const pre = e.preTokens ?? cur;
      before.set(i, pre);
      peak = Math.max(peak, pre);
      cur = e.postTokens ?? 0;
    }
    peak = Math.max(peak, cur);
    values[i] = cur;
  });
  return {
    values,
    before,
    max: Math.max(map.totals.maxContext ?? 0, peak, 1),
  };
}

// --- time axis ----------------------------------------------------------------

export const TIMELINE_AXES = ['steps', 'time'] as const;
export type TimelineAxis = (typeof TIMELINE_AXES)[number];
export const isTimelineAxis = (a: unknown): a is TimelineAxis =>
  typeof a === 'string' && (TIMELINE_AXES as readonly string[]).includes(a);

/** Idle gaps (a user turn, or any gap over 20 min) are capped to 5 min on
 * the time axis so one lunch break does not flatten the session. */
export const IDLE_GAP_MS = 20 * 60 * 1000;
export const IDLE_CAP_MS = 5 * 60 * 1000;

/** x position in [0, 1] of every event, by step or by compressed time. */
export function timePositions(
  events: SessionMapEvent[],
  axis: TimelineAxis
): Float64Array {
  const N = events.length;
  const pos = new Float64Array(N);
  if (N < 2) return pos;
  if (axis === 'steps') {
    for (let i = 0; i < N; i++) pos[i] = i / (N - 1);
    return pos;
  }
  let acc = 0;
  for (let i = 1; i < N; i++) {
    let g = Math.max(0, events[i].t - events[i - 1].t);
    if (events[i].kind === 'user' || g > IDLE_GAP_MS)
      g = Math.min(g, IDLE_CAP_MS);
    acc += g;
    pos[i] = acc;
  }
  if (acc) for (let i = 0; i < N; i++) pos[i] /= acc;
  return pos;
}

/** Nearest event index to the fraction `f` (positions are non-decreasing). */
export function indexAtFraction(pos: Float64Array, f: number): number {
  const N = pos.length;
  if (!N) return 0;
  let lo = 0;
  let hi = N - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (pos[mid] < f) lo = mid + 1;
    else hi = mid;
  }
  return lo > 0 && Math.abs(pos[lo - 1] - f) < Math.abs(pos[lo] - f)
    ? lo - 1
    : lo;
}

// --- signals at the playhead / whole session -------------------------------

export interface SignalCounts {
  errors: number;
  bySource: Record<ErrorSource, number>;
  byClass: Record<string, number>;
  retries: number;
  delays: number;
  delayMs: number;
  compactions: number;
  /** main-agent context at the playhead */
  ctxNow: number;
}

const zeroSources = (): Record<ErrorSource, number> => ({
  tool: 0,
  mcp: 0,
  llm: 0,
  hook: 0,
  orchestration: 0,
});

export function signalsAt(events: SessionMapEvent[], p: number): SignalCounts {
  const c: SignalCounts = {
    errors: 0,
    bySource: zeroSources(),
    byClass: {},
    retries: 0,
    delays: 0,
    delayMs: 0,
    compactions: 0,
    ctxNow: 0,
  };
  for (let i = 0; i <= p && i < events.length; i++) {
    const e = events[i];
    if (e.err) {
      c.errors++;
      c.bySource[errSource(e)]++;
      const k = errClassOf(e);
      c.byClass[k] = (c.byClass[k] ?? 0) + 1;
    }
    if (e.retryOf != null) c.retries++;
    if (e.kind === 'delay') {
      c.delays++;
      c.delayMs += e.ms ?? 0;
    }
    if (e.kind === 'compaction') {
      c.compactions++;
      if (e.agent === 'main') c.ctxNow = e.postTokens ?? 0;
    }
    if (e.kind === 'llm' && e.agent === 'main') c.ctxNow = ctxOf(e);
  }
  return c;
}

export interface SessionSignals {
  errors: number;
  bySource: Record<ErrorSource, number>;
  retries: number;
  delays: number;
  delayMs: number;
  compactions: number;
  maxContext: number;
  latencyP50: number | null;
  latencyP95: number | null;
}

function percentile(sorted: number[], q: number): number | null {
  if (!sorted.length) return null;
  const idx = Math.min(
    sorted.length - 1,
    Math.max(0, Math.ceil(q * sorted.length) - 1)
  );
  return sorted[idx];
}

/** Whole-session signals: the map's totals when the CLI sent them, computed
 * from events otherwise (older maps). */
export function sessionSignals(map: SessionMap): SessionSignals {
  const all = signalsAt(map.events, map.events.length - 1);
  const t = map.totals;
  const bySource = zeroSources();
  const src = Object.keys(t.errorsBySource).length
    ? t.errorsBySource
    : all.bySource;
  for (const k of ERROR_SOURCES) bySource[k] = src[k] ?? 0;
  const latencies = map.events
    .filter((e) => e.kind === 'llm' && e.latency != null)
    .map((e) => e.latency ?? 0)
    .sort((a, b) => a - b);
  return {
    errors: t.errors || all.errors,
    bySource,
    retries: t.retries ?? all.retries,
    delays: t.delays ?? all.delays,
    delayMs: t.delayMs ?? all.delayMs,
    compactions: t.compactions ?? all.compactions,
    maxContext: t.maxContext ?? contextSeries(map).max,
    latencyP50: t.llmLatencyP50 ?? percentile(latencies, 0.5),
    latencyP95: t.llmLatencyP95 ?? percentile(latencies, 0.95),
  };
}

// --- detail rows ------------------------------------------------------------

export type RowTone = 'bad' | 'warn';
export interface DetailRow {
  k: string;
  v: string;
  tone?: RowTone;
  /** Index of an event the row points at; the card renders it as a seek button. */
  seek?: number;
}

const SLOW_TOOL_MS = 30_000;
const SLOW_LLM_MS = 60_000;
export const LONG_DELAY_MS = 120_000;

/**
 * Per-kind detail rows for the event inspector (`compact` = the timeline
 * tooltip: agent only for subagents, response text only when present).
 */
export function detailRows(
  map: SessionMap,
  i: number,
  cumCost: Float64Array,
  ordinals: Map<string, number>,
  compact: boolean
): DetailRow[] {
  const e = map.events[i];
  const rows: DetailRow[] = [];
  const add = (k: string, v: string, tone?: RowTone) =>
    rows.push({ k, v, tone });
  if (e.kind === 'llm') {
    add('context in', fmtTok(ctxOf(e)));
    add('fresh input', fmtTok(e.in ?? 0));
    add('cache write', fmtTok(e.cw ?? 0));
    add('cache read', fmtTok(e.cr ?? 0));
    add('output', fmtTok(e.out ?? 0));
    add('cost', fmtUsd(e.cost ?? 0));
    add('session so far', fmtUsd(cumCost[i] ?? 0));
    if (e.latency != null)
      add(
        'latency',
        fmtDur(e.latency),
        e.latency >= SLOW_LLM_MS ? 'warn' : undefined
      );
    if (e.stop)
      add('stop reason', e.stop, e.stop === 'max_tokens' ? 'warn' : undefined);
    add('thinking', e.thinking ? 'yes' : 'no');
    add('tool calls asked', String(e.toolCalls ?? 0));
    if (!compact || e.textChars)
      add('response text', `${fmtTok(e.textChars ?? 0)} chars (not stored)`);
  } else if (e.kind === 'compaction') {
    add('trigger', e.trigger || 'auto');
    add('before', fmtTok(e.preTokens ?? 0));
    add('after', fmtTok(e.postTokens ?? 0));
    add('dropped', fmtTok((e.preTokens ?? 0) - (e.postTokens ?? 0)));
    if (e.ms) add('took', fmtDur(e.ms));
  } else if (e.kind === 'error') {
    const src = errSource(e);
    add('source', ERROR_SOURCE_LABEL[src]);
    add('class', errClassOf(e), 'bad');
    if (e.hook) add('hook', e.hook);
    if (e.source === 'hook')
      add('blocking', e.errClass === 'hook_blocking' ? 'yes' : 'no');
    // `ref` is a seq; a scoped map may not hold the event it names.
    const cause = map.events.findIndex((x, j) => (x.seq ?? j) === e.ref);
    if (e.ref != null && cause >= 0)
      rows.push({
        k: 'triggered by',
        v: `#${cause} ${describeEvent(map, map.events[cause]).title}`,
        seek: cause,
      });
    if (e.retry) add('retried', 'yes');
  } else if (e.kind === 'delay') {
    add('cause', DELAY_CAUSE_LABEL[e.cause ?? ''] ?? e.cause ?? 'delay');
    add(
      'duration',
      fmtDur(e.ms ?? 0),
      (e.ms ?? 0) >= LONG_DELAY_MS ? 'bad' : 'warn'
    );
    if (e.ref != null) add('event', `#${e.ref}`);
  } else if (e.kind === 'user') {
    add('length', `${e.chars ?? 0} chars`);
  } else {
    if (e.verb && e.tool === 'Bash') add('command', e.verb);
    if (e.dur != null)
      add(
        'duration',
        fmtDur(e.dur),
        e.dur >= SLOW_TOOL_MS ? 'warn' : undefined
      );
    if (e.outBytes != null) add('output', `${fmtTok(e.outBytes)}B`);
    if (e.err) {
      add('error', e.errClass ? errClassLabel(e.errClass) : 'failed', 'bad');
      if (e.exitCode != null) add('exit code', String(e.exitCode), 'bad');
    }
    if (e.retryOf != null)
      add('retry', `attempt ${e.attempt ?? 2} · after #${e.retryOf}`, 'warn');
  }
  if (!compact || e.agent !== 'main')
    add('agent', agentLabel(map, e.agent, ordinals));
  return rows;
}
