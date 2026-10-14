import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { parseAsStringEnum, useQueryState } from 'nuqs';
import type { SessionMap } from '@/core/schema';
import { cn } from '@/lib/utils';
import { resolveCssColor } from '@/core/eventColors';
import type { Category } from '@/core/sessionLayout';
import { llmTokens } from '@/core/sessionMapModel';
import {
  sessionTasks,
  taskNoun,
  type LaneKey,
  type TaskWindow,
} from '@/core/sessionScope';
import {
  TIMELINE_AXES,
  contextSeries,
  indexAtFraction,
  timePositions,
  type TimelineAxis,
} from '@/core/sessionSignals';
import {
  TL,
  buildLanes,
  drawAgentRows,
  drawAxis,
  drawContext,
  drawEvents,
  drawLaneFrames,
  drawPin,
  drawPlayhead,
  drawShade,
  drawTasks,
  laneSnap,
  type DrawEnv,
  type Lane,
} from '@/core/timelineDraw';
import { TimelineTooltip } from './TimelineTooltip';

/** The timeline's x axis (`axis` URL param): steps or wall-clock time. */
export function useTimelineAxis() {
  return useQueryState(
    'axis',
    parseAsStringEnum<TimelineAxis>([...TIMELINE_AXES]).withDefault('steps')
  );
}

interface Props {
  map: SessionMap;
  p: number;
  cumCost: Float64Array;
  ordinals: Map<string, number>;
  onSeek: (index: number) => void;
  /** the lanes to draw (`timelineLanes`) */
  lanes: ReadonlySet<LaneKey>;
  /** the dock's selected categories: bright on the Events bar */
  selected: ReadonlySet<Category>;
  /** selected task, highlighted on the task lane */
  task?: number | null;
  /** clicking a task bracket selects that task (click again to clear) */
  onSelectTask?: (n: number) => void;
  /** inclusive event window to keep bright; everything outside it is shaded
   * (an open subagent's [first, last] window on the whole-session timeline) */
  shade?: { start: number; end: number } | null;
  /** one row per subagent, in start order ("n · type") */
  subRows: readonly { id: string; label: string }[];
  /** the Subagents row is expanded into `subRows` */
  subsOpen: boolean;
  onToggleSubs: () => void;
  /** a subagent row's label opens that subagent's view */
  onOpenSubagent?: (id: string) => void;
  /** the open subagent (its row label is highlighted) */
  activeAgent?: string | null;
  /** the pinned event: a dashed rule and an emphasised mark */
  pinned?: number | null;
  /** an event to emphasise (hovered in the events list) */
  highlight?: number | null;
}

interface Hover {
  i: number;
  x: number;
  /** y in the non-scrolling wrapper's coordinates, where the tooltip is
   * drawn so the lane scrollport cannot clip it */
  vy: number;
}

/** Bumps whenever the theme class / attribute on <html> changes, so the
 * canvas re-resolves its CSS variables. */
function useThemeVersion(): number {
  const [v, setV] = useState(0);
  useEffect(() => {
    const mo = new MutationObserver(() => setV((x) => x + 1));
    mo.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['class', 'data-theme', 'style'],
    });
    return () => mo.disconnect();
  }, []);
  return v;
}

/** Width of an element, tracked with a ResizeObserver. */
function useWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      setWidth(Math.floor(entries[0]?.contentRect.width ?? 0));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

/** Scales the timeline draws against (largest prompt, delay, request). */
function useScales(map: SessionMap) {
  return useMemo(() => {
    let maxCost = 1e-9;
    let maxTok = 1;
    let maxChars = 1;
    let maxDelayMs = 1000;
    for (const e of map.events) {
      if (e.kind === 'user') maxChars = Math.max(maxChars, e.chars ?? 0);
      if (e.kind === 'delay') maxDelayMs = Math.max(maxDelayMs, e.ms ?? 0);
      if (e.kind !== 'llm') continue;
      maxCost = Math.max(maxCost, e.cost ?? 0);
      maxTok = Math.max(maxTok, llmTokens(e));
    }
    return { maxCost, maxTok, maxChars, maxDelayMs, ctx: contextSeries(map) };
  }, [map]);
}

const hasHtmlLabel = (l: Lane) => l.key === 'subs' || l.key === 'sub';

/**
 * The canvas timeline: task brackets, the Events bar (every event, the
 * selected categories bright), the optional context lane, agent rows and the
 * playhead. Drag to scrub,
 * hover for details. It grows to its content; the dock scrolls it.
 */
export function SessionTimeline({
  map,
  p,
  cumCost,
  ordinals,
  onSeek,
  lanes: visible,
  selected,
  task = null,
  onSelectTask,
  shade = null,
  subRows,
  subsOpen,
  onToggleSubs,
  onOpenSubagent,
  activeAgent = null,
  pinned = null,
  highlight = null,
}: Props) {
  /** full "n · name" of the task bracket under the pointer (canvas title) */
  const [taskTip, setTaskTip] = useState<string | null>(null);
  const taskWindows = useMemo<TaskWindow[]>(
    () => sessionTasks(map).tasks,
    [map]
  );
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const outerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const width = useWidth(wrapRef);
  const [hover, setHover] = useState<Hover | null>(null);
  const themeVersion = useThemeVersion();
  const [axis] = useTimelineAxis();

  const events = map.events;
  const N = events.length;
  const noun = taskNoun(taskWindows);
  const { lanes, bottom } = useMemo(
    () => buildLanes(visible, subRows, subsOpen, noun.many),
    [visible, subsOpen, subRows, noun.many]
  );
  const height = bottom + TL.axisH;
  const pos = useMemo(() => timePositions(events, axis), [events, axis]);
  const scales = useScales(map);
  const plotW = Math.max(1, width - TL.labelW - TL.right);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !width || !N) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const cache = new Map<string, string>();
    const family = getComputedStyle(canvas).fontFamily || 'sans-serif';
    const g: DrawEnv = {
      ctx,
      col: (v) => {
        let c = cache.get(v);
        if (c == null) {
          c = resolveCssColor(canvas, v);
          cache.set(v, c);
        }
        return c;
      },
      x: (i) => TL.labelW + plotW * (pos[i] ?? 0),
      cw: Math.max(1.2, axis === 'steps' ? plotW / N : 2),
      width,
      plotW,
      bottom,
      sans: `10.5px ${family}`,
      mono: '9.5px ui-monospace, SFMono-Regular, Menlo, monospace',
      events,
    };
    const L = new Map(lanes.map((l) => [l.key, l]));

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.font = g.sans;
    ctx.textBaseline = 'middle';

    drawLaneFrames(g, lanes, hasHtmlLabel);
    const TK = L.get('tasks');
    if (TK) drawTasks(g, TK, taskWindows, task, map.session.startedAt);
    const C = L.get('ctx');
    if (C) drawContext(g, C, scales.ctx);
    const emph = new Set<number>();
    if (pinned != null) emph.add(pinned);
    if (highlight != null) emph.add(highlight);
    const E = L.get('events');
    if (E) drawEvents(g, E, selected, scales, emph);
    drawAgentRows(g, lanes);
    if (shade) drawShade(g, shade);
    drawPlayhead(g, p);
    if (pinned != null && pinned < N) drawPin(g, pinned);
    drawAxis(g, pos, map.session.startedAt, height);
  }, [
    width,
    height,
    plotW,
    N,
    p,
    pos,
    axis,
    lanes,
    bottom,
    events,
    scales,
    selected,
    map.session.startedAt,
    taskWindows,
    task,
    themeVersion,
    shade,
    pinned,
    highlight,
  ]);

  const idxFromX = (clientX: number) => {
    const r = canvasRef.current?.getBoundingClientRect();
    if (!r || N <= 1) return 0;
    const f = Math.max(0, Math.min(1, (clientX - r.left - TL.labelW) / plotW));
    return indexAtFraction(pos, f);
  };

  const taskUnder = (yy: number, i: number) => {
    const TK = lanes.find((l) => l.key === 'tasks');
    return TK && yy >= TK.y && yy <= TK.y + TK.h
      ? taskWindows.find((t) => i >= t.start && i <= t.end)
      : undefined;
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!N) return;
    const yy = e.clientY - e.currentTarget.getBoundingClientRect().top;
    const w = onSelectTask ? taskUnder(yy, idxFromX(e.clientX)) : undefined;
    if (w && onSelectTask) {
      onSelectTask(w.n);
      return;
    }
    dragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    setHover(null);
    onSeek(idxFromX(e.clientX));
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!N) return;
    if (dragging.current) {
      onSeek(idxFromX(e.clientX));
      return;
    }
    const r = e.currentTarget.getBoundingClientRect();
    const yy = e.clientY - r.top;
    const raw = idxFromX(e.clientX);
    const overTask = taskUnder(yy, raw);
    setTaskTip(
      overTask ? `${noun.one} ${overTask.n} · ${overTask.name}` : null
    );
    const ln = lanes.find((l) => yy >= l.y - 2 && yy <= l.y + l.h + 2);
    // The Events bar prefers a selected event near the pointer, else the
    // (dimmed) event right under it, so every mark has a tooltip.
    const i =
      snapIndex(events, raw, ln ? laneSnap(ln, selected) : undefined) ??
      (ln?.key === 'events' ? raw : null);
    if (i == null) {
      setHover(null);
      return;
    }
    const outerTop = outerRef.current?.getBoundingClientRect().top ?? r.top;
    setHover({ i, x: e.clientX - r.left, vy: yy + r.top - outerTop });
  };

  const endDrag = () => {
    dragging.current = false;
  };

  return (
    // The tooltip is drawn in this relative wrapper, outside the lanes'
    // scrollport, so it is never clipped by it.
    <div ref={outerRef} className="relative flex min-h-0 flex-col">
      <div className="min-h-0 overflow-y-auto overscroll-contain">
        <div ref={wrapRef} className="relative w-full">
          <canvas
            ref={canvasRef}
            className="block w-full cursor-ew-resize touch-none"
            style={{ height }}
            aria-label="Timeline: drag to scrub, hover for details"
            title={taskTip ?? undefined}
            role="img"
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onPointerLeave={() => {
              setHover(null);
              setTaskTip(null);
            }}
          />
          <AgentLaneLabels
            lanes={lanes}
            subsOpen={subsOpen}
            onToggleSubs={onToggleSubs}
            onOpenSubagent={onOpenSubagent}
            activeAgent={activeAgent}
          />
          {taskWindows.length > 1 ? (
            <ul className="sr-only" aria-label={`${noun.many} on the timeline`}>
              {taskWindows.map((w) => (
                <li key={w.n}>
                  {noun.one} {w.n}: {w.name}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </div>
      {hover && events[hover.i] ? (
        <TimelineTooltip
          map={map}
          i={hover.i}
          cumCost={cumCost}
          ordinals={ordinals}
          left={Math.max(4, Math.min(hover.x + 12, width - 264))}
          top={hover.vy}
        />
      ) : null}
    </div>
  );
}

/** Nearest event to `i` (within a small window) that `want` accepts; the
 * raw index when the lane has no snap rule, null when nothing is near. */
function snapIndex(
  events: SessionMap['events'],
  i: number,
  want: ((e: SessionMap['events'][number]) => boolean) | undefined
): number | null {
  if (!want) return i;
  const N = events.length;
  const span = Math.max(3, Math.ceil(N / 120));
  let best = -1;
  let bd = Infinity;
  for (let j = Math.max(0, i - span); j <= Math.min(N - 1, i + span); j++) {
    if (want(events[j]) && Math.abs(j - i) < bd) {
      bd = Math.abs(j - i);
      best = j;
    }
  }
  return best < 0 ? null : best;
}

/** HTML labels over the canvas for the agent rows that act on click: the
 * Subagents row expands / collapses, a subagent row opens its view. */
function AgentLaneLabels({
  lanes,
  subsOpen,
  onToggleSubs,
  onOpenSubagent,
  activeAgent,
}: {
  lanes: readonly Lane[];
  subsOpen: boolean;
  onToggleSubs: () => void;
  onOpenSubagent?: (id: string) => void;
  activeAgent: string | null;
}) {
  return (
    <>
      {lanes.filter(hasHtmlLabel).map((l) =>
        l.key === 'subs' ? (
          <button
            key={l.id}
            type="button"
            aria-expanded={subsOpen}
            onClick={onToggleSubs}
            title={
              subsOpen
                ? 'Collapse to one combined row'
                : 'Expand: one row per subagent'
            }
            className="absolute left-0 flex items-center gap-1 truncate rounded px-1 text-left text-[10.5px] font-semibold leading-none text-ds-text-default hover:bg-ds-gray-subtlest"
            style={{ top: l.y, height: l.h, width: TL.labelW - 6 }}
          >
            <span aria-hidden>{subsOpen ? '▾' : '▸'}</span>
            {l.label}
          </button>
        ) : (
          <button
            key={l.id}
            type="button"
            onClick={() => onOpenSubagent?.(l.id)}
            disabled={!onOpenSubagent}
            title={`Open subagent ${l.label}`}
            className={cn(
              'absolute left-0 truncate rounded pl-3 pr-1 text-left text-[10px] leading-none hover:bg-ds-gray-subtlest disabled:cursor-default',
              activeAgent === l.id
                ? 'font-semibold text-ds-brand-default'
                : 'text-ds-text-subtle'
            )}
            style={{ top: l.y, height: l.h, width: TL.labelW - 6 }}
          >
            {l.label}
          </button>
        )
      )}
    </>
  );
}
