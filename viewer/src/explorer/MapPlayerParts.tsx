import { useMemo, type ReactNode } from 'react';
import type {
  SessionExplorerSession,
  SessionMap,
} from '@/core/schema';
import { EmptyState } from '@/components/EmptyState';
import { agentToolLabel, sessionTitle } from './SessionList';
import { SessionMapSvg } from './SessionMapSvg';
import {
  ScopeStrip,
  SessionSidebar,
  SidebarRail,
  SubagentSidebar,
} from './SessionSidebar';
import {
  adjacentSubagent,
  adjacentTask,
  mapChipSelection,
  mapFocus,
  pathFocus,
  resolveSidebarTab,
  sidebarRailLabel,
  errorClickAction,
  wholeSessionErrors,
  taskErrors,
  sidebarTabs,
  stripState,
  subStripMeta,
  taskAt,
  taskIndexWindow,
  taskStripMeta,
  touchedGroups,
  windowProgress,
  type Category,
  type SidebarTab,
  type SubagentInfo,
} from '@/core/sessionLayout';
import { srcSeqOf, type TreeNode } from '@/core/sessionMapModel';
import { hasReplayEvents, taskNoun } from '@/core/sessionScope';
import type { ExplorerScope } from './useExplorerScope';
import type { MapModel } from './usePlaybackWindow';

/** Sidebar state owned by `SessionExplorer`, rendered by the map player
 * (session mode needs the loaded map) or by the explorer itself. */
export interface SidebarCtl {
  /** `list`: the sessions list; `session`: only the selected session */
  mode: 'list' | 'session';
  collapsed: boolean;
  setCollapsed: (collapsed: boolean) => void;
  showList: () => void;
  showSession: () => void;
  /** the sessions list element (list mode) */
  list: ReactNode;
}

/** Open the subagent `delta` places before/after the open one, if any. */
function stepSubagent(x: ExplorerScope, delta: number) {
  const next = x.sub ? adjacentSubagent(x.subs, x.sub, delta) : null;
  if (next) x.setLens(next);
}

/** The one sidebar: rail (collapsed), the open subagent, the sessions list,
 * or the selected session with its Tasks / Subagents / Insights tabs. */
export function ExplorerSidebar({
  x,
  model,
  row,
  sidebar,
  subInfo,
  tabParam,
  onTab,
  insights,
  onFocusCategory,
  p,
  onSeek,
  onJumpToTaskErrors,
}: {
  x: ExplorerScope;
  model: MapModel;
  row: SessionExplorerSession | null;
  sidebar: SidebarCtl;
  subInfo: SubagentInfo | null;
  tabParam: SidebarTab | null;
  onTab: (tab: SidebarTab) => void;
  insights: ReactNode;
  onFocusCategory?: (c: Category) => void;
  p: number;
  onSeek: (index: number) => void;
  /** Scope to a task, open its errors and land on the first one. */
  onJumpToTaskErrors?: (task: number | null, seq: number) => void;
}) {
  const { fullMap, tasks, subs } = x;
  if (sidebar.collapsed) {
    return (
      <SidebarRail
        label={sidebarRailLabel({
          subLabel: subInfo?.label ?? null,
          mode: sidebar.mode,
          sessionId: x.map.session.id,
          taskCount: tasks.length,
          noun: taskNoun(tasks),
        })}
        onExpand={() => sidebar.setCollapsed(false)}
      />
    );
  }
  if (subInfo)
    return <SubagentPane x={x} sub={subInfo} model={model} sidebar={sidebar} />;
  if (sidebar.mode === 'list') return <>{sidebar.list}</>;
  // Over the whole session, whatever the prompt or model scope, so the Errors
  // count and list never narrow. A row is addressed by its whole-session `seq`;
  // `onSelectError` seeks in place or re-scopes first when the scope hides it.
  const errs = wholeSessionErrors(fullMap);
  const playhead = x.map.events[p];
  const playheadSeq = playhead ? (srcSeqOf(playhead) ?? p) : null;
  // Over the whole session, like the rest of the task list.
  const errsByTask = taskErrors(fullMap);
  const tabs = sidebarTabs(tasks.length, subs.length, errs.total);
  const s = x.map.session;
  return (
    <SessionSidebar
      row={row}
      title={row ? sessionTitle(row) : `${agentToolLabel(s.agentTool)} session`}
      startedAt={fullMap.session.startedAt}
      durationMs={fullMap.session.durationMs}
      costUsd={fullMap.totals.costUsd}
      prCount={fullMap.prs.length}
      errors={errs.total}
      tasks={tasks}
      taskSums={x.taskSums}
      task={x.scope.task}
      onSelectTask={(n) => x.setScope((s0) => ({ ...s0, task: n }))}
      subs={subs}
      onOpenSub={x.setLens}
      tabs={tabs}
      tab={resolveSidebarTab(tabParam, tabs)}
      onTab={onTab}
      onShowList={sidebar.showList}
      onCollapse={() => sidebar.setCollapsed(true)}
      insights={insights}
      onFocusCategory={onFocusCategory}
      errorGroups={errs.groups}
      playheadSeq={playheadSeq}
      onSelectError={(err) => {
        const act = errorClickAction(err, x.map);
        if (act.kind === 'seek') onSeek(act.index);
        else onJumpToTaskErrors?.(act.task, act.seq);
      }}
      taskErrors={errsByTask}
      onJumpToTaskErrors={onJumpToTaskErrors}
    />
  );
}

/** The subagent view's sidebar: the helper and what it touched (from the
 * lensed map's tree). */
function SubagentPane({
  x,
  sub,
  model,
  sidebar,
}: {
  x: ExplorerScope;
  sub: SubagentInfo;
  model: MapModel;
  sidebar: SidebarCtl;
}) {
  const groups = useMemo(
    () => touchedGroups(model.tree, model.refs),
    [model.tree, model.refs]
  );
  return (
    <SubagentSidebar
      sub={sub}
      total={x.subs.length}
      tasks={x.tasks}
      startedAt={x.fullMap.session.startedAt}
      groups={groups}
      onBack={() => x.setLens('main')}
      onStep={(d) => stepSubagent(x, d)}
      canPrev={!!adjacentSubagent(x.subs, sub.id, -1)}
      canNext={!!adjacentSubagent(x.subs, sub.id, 1)}
      onCollapse={() => sidebar.setCollapsed(true)}
    />
  );
}

/** The radial map, filling the middle row. */
export function MapPane({
  map,
  model,
  p,
  onSeek,
  onNodeAction,
  filtered,
  filteredCopy,
  playing,
  selected,
  path = null,
}: {
  map: SessionMap;
  model: MapModel;
  p: number;
  onSeek: (i: number) => void;
  onNodeAction: (node: TreeNode) => boolean;
  /** a task / model / agent filter is set (for the empty-state copy) */
  filtered: boolean;
  /** the empty-state sentence naming the active filters */
  filteredCopy: string;
  /** caps the map to the recently-touched window while playback runs */
  playing: boolean;
  /** the dock's category chips: a narrowing selection lights its nodes */
  selected: ReadonlySet<Category>;
  /** event `seq`s of a picked review comment's causal path, lit over
   * everything else; null when no comment is picked */
  path?: readonly number[] | null;
}) {
  const chipFocus = useMemo(
    () => mapFocus(model.tree, model.refs, map.events, selected),
    [model.tree, model.refs, map.events, selected]
  );
  const onPath = useMemo(
    () =>
      path?.length
        ? pathFocus(model.tree, model.refs, map.events, path)
        : null,
    [model.tree, model.refs, map.events, path]
  );
  // A picked path wins over the chips: it is the more specific thing the
  // user just asked to see, and intersecting the two can empty the map.
  const focus = onPath ?? chipFocus;
  // ...and winning has to mean the chips stop narrowing the map at all, not
  // just that `focus` is overridden: the map filters its event refs by the
  // selection too (`mapChipSelection`).
  const chips = mapChipSelection(selected, onPath != null);
  return (
    <section className="flex h-full min-h-0 min-w-0 flex-col rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-3">
      {hasReplayEvents(map) ? (
        <SessionMapSvg
          onNodeAction={onNodeAction}
          map={map}
          tree={model.tree}
          nodes={model.nodes}
          refs={model.refs}
          p={p}
          onSeek={onSeek}
          playing={playing}
          focus={focus}
          selected={chips}
        />
      ) : (
        <EmptyState
          title={
            filtered
              ? 'Nothing in this scope'
              : 'Nothing recorded in this session'
          }
          description={
            filtered
              ? filteredCopy
              : 'The map has no events to replay.'
          }
          size="sm"
        />
      )}
    </section>
  );
}

/** The one-line task / subagent strip, floating above the timeline dock. */
export function TimelineStrip({
  x,
  subInfo,
  playing,
  p,
  collapsed,
}: {
  x: ExplorerScope;
  subInfo: SubagentInfo | null;
  playing: boolean;
  p: number;
  collapsed: boolean;
}) {
  const strip = stripState({
    task: x.scope.task,
    sub: x.sub,
    collapsed,
    playing,
    playheadTask: taskAt(x.map.events, p),
    taskCount: x.tasks.length,
  });
  const lastIdx = Math.max(0, x.map.events.length - 1);
  if (strip?.kind === 'sub' && subInfo) {
    return (
      <ScopeStrip
        lead={`${strip.playing ? '▶ Playing' : 'Subagent'} ${subInfo.index}/${x.subs.length}`}
        name={subInfo.label}
        meta={subStripMeta(x.fullMap.session.startedAt, subInfo)}
        progress={
          strip.playing ? windowProgress({ start: 0, end: lastIdx }, p) : null
        }
        onPrev={() => stepSubagent(x, -1)}
        onNext={() => stepSubagent(x, 1)}
        canPrev={!!adjacentSubagent(x.subs, subInfo.id, -1)}
        canNext={!!adjacentSubagent(x.subs, subInfo.id, 1)}
        onClear={() => x.setLens('main')}
        prevLabel="Previous subagent"
        nextLabel="Next subagent"
        clearLabel="Back to the main session"
      />
    );
  }
  if (strip?.kind !== 'task') return null;
  return (
    <TaskStrip
      x={x}
      n={strip.n}
      playing={strip.playing}
      followsPlayhead={strip.followsPlayhead}
      p={p}
      lastIdx={lastIdx}
    />
  );
}

function TaskStrip({
  x,
  n,
  playing,
  followsPlayhead,
  p,
  lastIdx,
}: {
  x: ExplorerScope;
  n: number;
  playing: boolean;
  followsPlayhead: boolean;
  p: number;
  lastIdx: number;
}) {
  const { tasks } = x;
  const t = tasks.find((w) => w.n === n);
  if (!t) return null;
  const select = (task: number | null) => x.setScope((s0) => ({ ...s0, task }));
  const { one } = taskNoun(tasks);
  const prev = adjacentTask(tasks, t.n, -1);
  const next = adjacentTask(tasks, t.n, 1);
  const win = followsPlayhead
    ? taskIndexWindow(x.map.events, t.n)
    : { start: 0, end: lastIdx };
  return (
    <ScopeStrip
      lead={`${playing ? '▶ Playing' : one} ${t.n}/${tasks.length}`}
      name={t.name}
      meta={taskStripMeta(x.fullMap.session.startedAt, t, x.taskSums.get(t.n))}
      progress={playing ? windowProgress(win, p) : null}
      onPrev={() => select(prev)}
      onNext={() => select(next)}
      canPrev={prev != null && prev !== t.n}
      canNext={next != null && next !== t.n}
      onClear={followsPlayhead ? null : () => select(null)}
      prevLabel={`Previous ${one.toLowerCase()}`}
      nextLabel={`Next ${one.toLowerCase()}`}
      clearLabel={`Clear the ${one.toLowerCase()} filter`}
    />
  );
}
