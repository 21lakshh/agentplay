/**
 * Event-kind colours for the Session Explorer, all from the dataviz palette.
 * Kinds are identity, so they take SERIES slots in a fixed order; the two low-salience kinds (generic tool, web) fold into
 * SERIES_OTHER rather than a generated 9th hue. Errors and compaction are
 * state, so they wear STATUS. The LLM cost row is a single series: PRIMARY.
 *
 * Every value is a `var(--…)` string: SVG uses it directly, Canvas resolves it
 * through `resolveCssColor` so light and dark both follow the theme.
 */
import {
  CHROME,
  PRIMARY,
  PRIMARY_ACCENT,
  SERIES,
  SERIES_OTHER,
  STATUS,
} from '@/components/dataviz';
import type { SessionMapEvent } from './schema';
import {
  ERROR_SOURCE_LABEL,
  type ErrorSource,
  type NodeType,
  type ToolCategory,
  type TouchLevel,
} from './sessionMapModel';
import type { Category } from './sessionLayout';

export interface KindMeta {
  label: string;
  color: string;
}

export const KIND_META: Record<string, KindMeta> = {
  verify: { label: 'Verify', color: SERIES[0] },
  read: { label: 'Read', color: SERIES[1] },
  mcp: { label: 'MCP', color: SERIES[2] },
  exec: { label: 'Exec', color: SERIES[3] },
  edit: { label: 'Edit', color: SERIES[4] },
  search: { label: 'Search', color: SERIES[5] },
  subagent: { label: 'Subagent', color: SERIES[6] },
  skill: { label: 'Skill', color: SERIES[7] },
  web: { label: 'Web', color: SERIES_OTHER },
  tool: { label: 'Tool', color: SERIES_OTHER },
  llm: { label: 'LLM request', color: PRIMARY },
  user: { label: 'User turn', color: 'var(--ds-text-subtle)' },
  compaction: { label: 'Compaction', color: STATUS.critical },
  error: { label: 'Error signal', color: STATUS.critical },
  delay: { label: 'Delay', color: STATUS.warning },
  // Outputs + model switches: no new hues, each reuses the
  // identity colour of the kind it is closest to.
  commit: { label: 'Commit', color: SERIES[0] },
  pr_create: { label: 'PR opened', color: PRIMARY_ACCENT },
  pr_merge: { label: 'PR merged', color: PRIMARY_ACCENT },
  pr_close: { label: 'PR closed', color: 'var(--ds-text-subtle)' },
  pr_comment: { label: 'PR comment', color: SERIES_OTHER },
  ticket: { label: 'Ticket', color: SERIES[2] },
  artifact: { label: 'Artifact', color: SERIES[5] },
  skill_write: { label: 'Skill written', color: SERIES[7] },
  model_switch: { label: 'Model switch', color: PRIMARY },
};

export const kindMeta = (kind: string): KindMeta =>
  KIND_META[kind] ?? KIND_META.tool;

/** `events[].cat` colour for the tool-category breakdown: reuses the
 * KIND_META colour of the closest-matching kind rather than a new hue.
 * `test` and `build` share `verify`'s colour, matching their shared label. */
export const CAT_COLOR: Record<ToolCategory, string> = {
  file: KIND_META.read.color,
  shell: KIND_META.exec.color,
  vcs: KIND_META.mcp.color,
  test: KIND_META.verify.color,
  build: KIND_META.verify.color,
  cloud: KIND_META.subagent.color,
  http: KIND_META.web.color,
  fs: SERIES_OTHER,
  package: KIND_META.skill.color,
  mcp: KIND_META.mcp.color,
  subagent: KIND_META.subagent.color,
  skill: KIND_META.skill.color,
  web: KIND_META.web.color,
  tool: SERIES_OTHER,
};
export const catColor = (cat: string): string =>
  CAT_COLOR[cat as ToolCategory] ?? SERIES_OTHER;

/** Map colour by touch level: seen / read / edited. */
export const TOUCH_COLOR: Record<TouchLevel, string> = {
  1: KIND_META.search.color,
  2: KIND_META.read.color,
  3: KIND_META.edit.color,
};

export const NODE_COLOR: Partial<Record<NodeType, string>> = {
  repo: 'var(--ds-text-default)',
  group: 'var(--ds-text-subtle)',
  dir: 'var(--ds-text-subtle)',
  place: SERIES_OTHER,
  server: KIND_META.mcp.color,
  mcptool: KIND_META.mcp.color,
  skill: KIND_META.skill.color,
  agent: KIND_META.subagent.color,
  web: KIND_META.web.color,
  model: PRIMARY,
  outgroup: 'var(--ds-text-subtle)',
  output: SERIES[0],
};

export const nodeColor = (n: {
  type: NodeType;
  src?: ErrorSource;
  kind?: string;
}): string =>
  (n.type === 'errsrc' || n.type === 'errclass') && n.src
    ? ERROR_SOURCE_COLOR[n.src]
    : n.type === 'output' && n.kind
      ? kindMeta(n.kind).color
      : (NODE_COLOR[n.type] ?? 'var(--ds-border-subtler)');

export const ERROR_COLOR = STATUS.critical;

/** Error sources are state first (tool failures critical, LLM/API trouble
 * warning); MCP, hook and orchestration keep the identity of the thing that
 * failed so the error lane reads against the map. */
export const ERROR_SOURCE_COLOR: Record<ErrorSource, string> = {
  tool: STATUS.critical,
  mcp: KIND_META.mcp.color,
  llm: STATUS.warning,
  hook: KIND_META.skill.color,
  orchestration: KIND_META.subagent.color,
};

/** Kind style for an event: an `error` event takes its source's colour. */
export function kindStyle(e: SessionMapEvent): KindMeta {
  if (e.kind === 'error') {
    const src = (Object.keys(ERROR_SOURCE_COLOR) as ErrorSource[]).find(
      (k) => k === e.source
    );
    const s: ErrorSource = src ?? 'tool';
    return {
      label: `Error · ${ERROR_SOURCE_LABEL[s]}`,
      color: ERROR_SOURCE_COLOR[s],
    };
  }
  return kindMeta(e.kind);
}

/** Main-agent context line on the timeline. */
export const CONTEXT_COLOR = KIND_META.read.color;
/** Prompt marks on the timeline: lime (light #65a30d, dark #a3e635). */
export const PROMPT_COLOR = 'var(--ds-session-prompt)';
/** Swatch of each dock category (chips, Events bar, events list). No new
 * hues: problems wear STATUS, activity reuses the kind it names; model
 * requests and their tokens are PRIMARY, cost PRIMARY_ACCENT (SERIES[5]
 * stays the map's "seen" file colour). */
export const CATEGORY_COLOR: Record<Category, string> = {
  prompt: PROMPT_COLOR,
  error: STATUS.critical,
  delay: STATUS.warning,
  out: SERIES[0],
  edit: KIND_META.edit.color,
  explore: KIND_META.read.color,
  mcp: KIND_META.mcp.color,
  skill: KIND_META.skill.color,
  tool: KIND_META.exec.color,
  model: KIND_META.llm.color,
  cost: PRIMARY_ACCENT,
  tokens: PRIMARY,
};
export const TRAIL_COLOR = PRIMARY_ACCENT;
/** Single-series meter fill (agents cost share). */
export const PANEL_BAR_COLOR = PRIMARY;
export const GRID_COLOR = CHROME.grid;
export const AXIS_COLOR = CHROME.axis;

/** Faint fill for nodes touched before the active window. */
export const FAINT_FILL = 'var(--ds-gray-subtler)';
/** Untouched (hollow) node fill + ring. */
export const HOLLOW_FILL = 'var(--ds-surface-white)';
export const HOLLOW_RING = 'var(--ds-border-subtler)';

/**
 * Resolve `var(--x)` (or a bare `--x`) to the computed colour on `el`, so a
 * Canvas can paint with the current theme. Falls back to the input for a
 * literal colour.
 */
export function resolveCssColor(el: Element, value: string): string {
  const m = /^var\((--[^),\s]+)/.exec(value.trim());
  const name = m ? m[1] : value.startsWith('--') ? value : null;
  if (!name) return value;
  const v = getComputedStyle(el).getPropertyValue(name).trim();
  return v || 'currentColor';
}
