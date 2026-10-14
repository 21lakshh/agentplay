import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from 'react';
import { parseAsString, useQueryState } from 'nuqs';
import type { SessionMap } from '@/core/schema';
import { Button } from '@/components/ui/button';
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group';
import { CONTEXT_COLOR, KIND_META } from '@/core/eventColors';
import { TimelineStrip } from './MapPlayerParts';
import { PlaybackControls } from './PlaybackControls';
import {
  CATEGORIES,
  DEFAULT_CATEGORIES,
  DOCK_MIN_HEIGHT,
  categoryIndices,
  clampDockHeight,
  dockOverflow,
  getDockHeight,
  getDockHeightServer,
  lensSourceIndex,
  matchesAnyCategory,
  nearestLensIndex,
  setDockHeight,
  stepEvent,
  subagentLanes,
  subscribeDockHeight,
  toggleCategory,
  type Category,
  type SubagentInfo,
} from '@/core/sessionLayout';
import { clockAt, cumulativeCost, subagentOrdinal } from '@/core/sessionMapModel';
import { timelineLanes } from '@/core/sessionScope';
import { SessionTimeline, useTimelineAxis } from './SessionTimeline';
import { isTimelineAxis } from '@/core/sessionSignals';
import { CategoryChips, EventsPanel, LaneToggle } from './TimelineEvents';
import { optionalLanesHeight } from '@/core/timelineDraw';
import type { EventCategories } from './useEventCategories';
import type { ExplorerScope } from './useExplorerScope';
import type { MapModel, PlaybackWindow } from './usePlaybackWindow';

/**
 * What the timeline replays: the playback map itself, or (in a subagent
 * view) the whole session with the subagent's replay mapped onto it.
 * `allow` limits the event chips / list to the subagent's own events.
 */
function useTimelineSource(
  x: ExplorerScope,
  playback: PlaybackWindow,
  model: MapModel
) {
  const { map, scopedMap, sub } = x;
  const { pb, seek } = playback;
  const lens = useMemo(() => {
    if (!sub) return null;
    const src = lensSourceIndex(scopedMap.events, sub);
    const set = new Set(src);
    return {
      src,
      allow: (i: number) => set.has(i),
      cumCost: cumulativeCost(scopedMap.events),
      ordinals: subagentOrdinal(scopedMap),
    };
  }, [sub, scopedMap]);
  if (!lens)
    return {
      map,
      p: pb.p,
      seek,
      cumCost: model.cumCost,
      ordinals: model.ordinals,
      shade: null,
      allow: undefined,
    };
  const { src } = lens;
  return {
    map: scopedMap,
    p: src[pb.p] ?? 0,
    seek: (i: number) => seek(nearestLensIndex(src, i)),
    cumCost: lens.cumCost,
    ordinals: lens.ordinals,
    shade: src.length ? { start: src[0], end: src[src.length - 1] } : null,
    allow: lens.allow,
  };
}

/** The persisted dock height floor (null: just its content height). */
function useDockHeight() {
  return useSyncExternalStore(
    subscribeDockHeight,
    getDockHeight,
    getDockHeightServer
  );
}

/**
 * The timeline dock at the bottom of the explorer: a resize grip, one
 * header row (playback, the category chips with steppers, the Subagents /
 * Context lane toggles, axis, the events list)
 * and the lanes. It is as tall as its lanes, so they never scroll inside
 * it; optional lanes extend the page below the fold instead. A
 * dragged height is remembered as a floor.
 */
export function TimelineDock({
  x,
  playback,
  model,
  cats,
  onSelectTask,
  subInfo,
  collapsed,
  onOverflow,
  columnExtra,
}: {
  x: ExplorerScope;
  playback: PlaybackWindow;
  model: MapModel;
  /** the category chips' selection and the Context lane toggle */
  cats: EventCategories;
  onSelectTask: (n: number) => void;
  subInfo: SubagentInfo | null;
  collapsed: boolean;
  /** how many px the optional lanes grow the dock past its budget
   * (`dockOverflow`), for the explorer to grow the column by so the map
   * doesn't shrink to make room */
  onOverflow?: (px: number) => void;
  /** what the explorer actually applied from the last `onOverflow` call,
   * echoed back so the measurement effect reruns once the column's own
   * height has caught up (see the effect's comment) */
  columnExtra?: number | null;
}) {
  const tl = useTimelineSource(x, playback, model);
  const { pb, dispatch } = playback;
  const dockRef = useRef<HTMLElement>(null);
  const height = useDockHeight();
  const { selected } = cats;
  const [subsOpen, setSubsOpen] = useState(false);
  // Off by default: a whole agent-rows lane, not a point-event category,
  // so it is gated independently of the category chips.
  const [subsOn, setSubsOn] = useState(false);
  const [listOpen, setListOpen] = useState(false);
  const [hoverRow, setHoverRow] = useState<number | null>(null);
  // Local pin, tied to the map it indexes (a new scope drops it).
  const [pin, setPin] = useState<{ map: SessionMap; i: number } | null>(null);
  const pinned = pin && pin.map === tl.map ? pin.i : null;
  const [, setPanel] = useQueryState('panel', parseAsString);

  const events = tl.map.events;
  const { allow } = tl;
  const lists = useMemo(
    () =>
      Object.fromEntries(
        CATEGORIES.map((c) => [c, categoryIndices(events, c, allow)])
      ) as Record<Category, number[]>,
    [events, allow]
  );
  const rows = useMemo(() => {
    const out: number[] = [];
    events.forEach((e, i) => {
      if ((!allow || allow(i)) && matchesAnyCategory(e, selected)) out.push(i);
    });
    return out;
  }, [events, allow, selected]);
  const visibleLanes = useMemo(
    () => timelineLanes({ context: cats.context, subagents: subsOn }),
    [cats.context, subsOn]
  );
  const subRows = useMemo(
    () => subagentLanes(x.subs, events),
    [x.subs, events]
  );
  const optionalH = useMemo(
    () => optionalLanesHeight(visibleLanes, subRows, subsOpen),
    [visibleLanes, subRows, subsOpen]
  );
  // Measured, not computed: the dock's own chrome (header rows, padding)
  // decides its rendered height (a narrower window can wrap the header),
  // and the explorer column's height bounds the saved floor.
  const [box, setBox] = useState<{ dockH: number; columnH: number } | null>(
    null
  );
  // Synchronous (useLayoutEffect, before paint), not a ResizeObserver: every
  // state below already re-renders the dock at its new height on its own,
  // so re-measuring on the same triggers is exactly as timely and never
  // depends on a notification the observer might coalesce, throttle or (in
  // at least one real browser instance, confirmed by hand) never deliver at
  // all. A plain `resize` listener catches the one case none of this state
  // can: the window narrowing enough to wrap the header row.
  //
  // `columnExtra` (what the explorer actually applied last, echoed back as
  // a prop) closes the other case: shrinking the optional lanes measures
  // before `onOverflow` has told the explorer to shrink the column back,
  // so this first pass still sees its old, larger height and `floor` can
  // exceed the true cap. Once the explorer applies the smaller height and
  // that change comes back down as `columnExtra`, this reruns and the
  // second measurement is correct -- converges in at most one extra pass,
  // since a converged `overflow` reported back is a no-op for the explorer.
  useLayoutEffect(() => {
    const measure = () => {
      const el = dockRef.current;
      const column = el?.parentElement;
      if (!el || !column) return;
      setBox({
        dockH: el.getBoundingClientRect().height,
        columnH: column.clientHeight,
      });
    };
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, [
    subsOpen,
    subsOn,
    cats.context,
    collapsed,
    height,
    optionalH,
    columnExtra,
  ]);
  // A floor saved in a taller window is re-clamped to this one, so it can
  // never outgrow the explorer and squeeze the map.
  const floor =
    height != null && box ? clampDockHeight(height, box.columnH) : height;
  // How far the optional lanes grow the dock past its budget. Reported to
  // the explorer (onOverflow), which grows the column by the same amount
  // instead of letting the map's flex row shrink to make room.
  const overflow = box ? dockOverflow(box.dockH, optionalH, floor) : 0;
  // Scrolling a newly-grown lane into view is the explorer's job
  // (onOverflow drives it there, once the column has actually grown).
  useEffect(() => onOverflow?.(overflow), [overflow, onOverflow]);

  const pinEvent = (i: number) => {
    setPin({ map: tl.map, i });
    tl.seek(i);
    void setPanel('event');
  };
  // A pinned event the new selection no longer covers is unpinned.
  const select = (next: ReadonlySet<Category>) => {
    cats.setSelected(next);
    if (pinned != null && !matchesAnyCategory(events[pinned], next))
      setPin(null);
  };
  const onToggleSubsOn = () => {
    // Turning it off also collapses it, so re-enabling always starts
    // collapsed. Both updates are queued from the handler (not from inside
    // an updater, which React may replay).
    if (subsOn) setSubsOpen(false);
    setSubsOn(!subsOn);
  };
  const onStep = (c: Category, dir: 1 | -1) => {
    const i = stepEvent(lists[c], pinned, tl.p, dir);
    if (i != null) pinEvent(i);
  };
  const closeList = useCallback(() => {
    setListOpen(false);
    setHoverRow(null);
  }, []);

  return (
    <section
      ref={dockRef}
      aria-label="Timeline"
      // Sized by its lanes, never clipped: turning on Subagents / Context
      // grows the dock, and `onOverflow` tells the explorer column to grow
      // by the same amount, so the map keeps its size and the page scrolls
      // -- no scrollbar inside the dock. A dragged height is a floor.
      className="relative flex min-h-0 flex-none flex-col rounded-xl border border-ds-border-subtlest bg-ds-surface-white px-2.5 pb-1.5 pt-3"
      style={{ minHeight: floor ?? undefined }}
    >
      <DockGrip dockRef={dockRef} height={height} />
      <div className="pointer-events-none absolute bottom-full left-0 z-20 mb-1.5 max-w-[60%] [&>*]:pointer-events-auto [&>*]:shadow-md">
        <TimelineStrip
          x={x}
          subInfo={subInfo}
          playing={pb.playing}
          p={pb.p}
          collapsed={collapsed}
        />
      </div>
      {listOpen ? (
        <EventsPanel
          map={tl.map}
          rows={rows}
          selected={selected}
          ordinals={tl.ordinals}
          pinned={pinned}
          onPin={pinEvent}
          onHover={setHoverRow}
          onClose={closeList}
        />
      ) : null}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 pb-1.5">
        <PlaybackControls
          state={pb}
          clock={clockAt(
            x.map.session.startedAt,
            x.map.events[pb.p]?.t ?? 0
          ).slice(0, 5)}
          elapsedMs={x.map.events[pb.p]?.t ?? 0}
          durationMs={x.map.session.durationMs}
          onStart={() => playback.seek(0)}
          onToggle={() => dispatch({ type: 'toggle' })}
          onCycleSpeed={() => dispatch({ type: 'cycleSpeed' })}
        />
        <CategoryChips
          lists={lists}
          selected={selected}
          pinned={pinned}
          onToggle={(c) => select(toggleCategory(selected, c))}
          onStep={onStep}
          onReset={() => select(new Set(DEFAULT_CATEGORIES))}
        />
        <span aria-hidden className="h-4 w-px shrink-0 bg-ds-border-subtler" />
        <LaneToggle
          label="Subagents"
          color={KIND_META.subagent.color}
          count={subRows.length}
          on={subsOn}
          onToggle={onToggleSubsOn}
        />
        <LaneToggle
          label="Context"
          color={CONTEXT_COLOR}
          on={cats.context}
          onToggle={() => cats.setContext(!cats.context)}
        />
        <span className="flex-1" />
        <AxisToggle />
        <Button
          variant="outline"
          size="sm"
          aria-expanded={listOpen}
          aria-haspopup="listbox"
          onClick={() => (listOpen ? closeList() : setListOpen(true))}
        >
          List {listOpen ? '▾' : '▴'}
        </Button>
      </div>
      <SessionTimeline
        map={tl.map}
        p={tl.p}
        cumCost={tl.cumCost}
        ordinals={tl.ordinals}
        onSeek={tl.seek}
        shade={tl.shade}
        lanes={visibleLanes}
        selected={selected}
        task={x.scope.task}
        onSelectTask={onSelectTask}
        subRows={subRows}
        subsOpen={subsOpen}
        onToggleSubs={() => setSubsOpen((o) => !o)}
        onOpenSubagent={x.lens === 'main' ? x.setLens : undefined}
        activeAgent={x.sub}
        pinned={pinned}
        highlight={listOpen ? hoverRow : null}
      />
    </section>
  );
}

/** Steps / Time: what the timeline's x axis spaces events by. */
function AxisToggle() {
  const [axis, setAxis] = useTimelineAxis();
  return (
    <ToggleGroup
      value={[axis]}
      onValueChange={(v: unknown[]) => {
        const next = v.find(isTimelineAxis);
        if (next) void setAxis(next);
      }}
      aria-label="Timeline axis"
      className="shrink-0"
    >
      <ToggleGroupItem value="steps" className="px-2">
        Steps
      </ToggleGroupItem>
      <ToggleGroupItem value="time" className="px-2">
        Time
      </ToggleGroupItem>
    </ToggleGroup>
  );
}

const KEY_STEP = 24;

/** The dock's top-edge grip: drag to resize, double-click to reset to the
 * content height, ↑ / ↓ to resize from the keyboard. */
function DockGrip({
  dockRef,
  height,
}: {
  dockRef: RefObject<HTMLElement | null>;
  height: number | null;
}) {
  const drag = useRef<{ y0: number; h0: number; box: number } | null>(null);
  const container = () => dockRef.current?.parentElement?.clientHeight ?? 0;
  // The rendered height, not the saved one: the saved height is only a
  // floor, so the dock can be taller than it when its lanes need the room.
  const current = () =>
    dockRef.current?.getBoundingClientRect().height ?? height ?? 0;

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    drag.current = { y0: e.clientY, h0: current(), box: container() };
    e.currentTarget.setPointerCapture(e.pointerId);
    e.preventDefault();
  };
  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const d = drag.current;
    if (!d) return;
    setDockHeight(clampDockHeight(d.h0 - (e.clientY - d.y0), d.box), false);
  };
  const onPointerUp = () => {
    if (!drag.current) return;
    drag.current = null;
    setDockHeight(getDockHeight());
  };
  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    const d =
      e.key === 'ArrowUp' ? KEY_STEP : e.key === 'ArrowDown' ? -KEY_STEP : 0;
    if (!d) return;
    e.preventDefault();
    e.stopPropagation();
    setDockHeight(clampDockHeight(current() + d, container()));
  };
  return (
    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize the timeline: drag, or use the up and down arrow keys; double-click to reset"
      aria-valuemin={DOCK_MIN_HEIGHT}
      aria-valuenow={height ?? undefined}
      tabIndex={0}
      title="Drag to resize · double-click to reset"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      onDoubleClick={() => setDockHeight(null)}
      onKeyDown={onKeyDown}
      className="group absolute inset-x-0 top-0 flex h-3 cursor-ns-resize touch-none justify-center pt-1 outline-none"
    >
      <span className="h-[5px] w-14 rounded-full bg-ds-border-default transition-colors group-hover:bg-ds-brand-default group-focus-visible:bg-ds-brand-default motion-reduce:transition-none" />
    </div>
  );
}
