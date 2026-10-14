import { useMemo, useState, type KeyboardEvent, type MouseEvent } from 'react';
import type { SessionMap } from '@/core/schema';
import {
  OP_GLYPH,
  activeSet,
  ancestorsOf,
  lastTouch,
  leafCount,
  litSet,
  nextTouchOf,
  stateAt,
  touchesOfFile,
  type NodeRef,
  type NodeState,
  type SessionTree,
  type TreeNode,
} from '@/core/sessionMapModel';
import {
  ERROR_COLOR,
  FAINT_FILL,
  HOLLOW_FILL,
  HOLLOW_RING,
  KIND_META,
  TOUCH_COLOR,
  TRAIL_COLOR,
  kindStyle,
  nodeColor,
} from '@/core/eventColors';
import {
  labelChars,
  mapPadY,
  mapViewBox,
  truncateLabel,
  type LabelSpot,
} from '@/core/mapGeometry';
import {
  ALL_CATEGORIES,
  activeRefs,
  narrowsView,
  type Category,
} from '@/core/sessionLayout';

/** Leaf / branch label sizes (px in viewBox units). */
const LEAF_FONT = 13.5;
const BRANCH_FONT = 14;
const ACTIVE_LEN = 20;
const MONO = 'var(--font-mono, ui-monospace, monospace)';
/** Opacity multiplier for everything off the pinned leaf's ancestor chain,
 * while a leaf is pinned: noticeably faded but still legible. */
const PIN_DIM = 0.38;
/** Nodes coloured by their own touch state (everything else lights by path). */
const LEAFISH = new Set([
  'file',
  'place',
  'mcptool',
  'skill',
  'agent',
  'web',
  'errclass',
  'model',
  'output',
]);
/** Nodes whose label carries a trailing ×N -- entities called/touched more
 * than once that a chip selection can narrow down to, so the count reads
 * as "how many times", e.g. an MCP tool invoked repeatedly with different
 * args. */
const COUNTED = new Set([
  'file',
  'place',
  'errclass',
  'mcptool',
  'server',
  'skill',
  'model',
]);
const TYPE_LABEL: Record<string, string> = {
  model: 'model',
  outgroup: 'outputs',
  output: 'output',
  mcptool: 'MCP tool',
  server: 'MCP server',
  place: 'outside repo',
  agent: 'subagent',
  errclass: 'error class',
  errsrc: 'error source',
};

interface Props {
  map: SessionMap;
  tree: SessionTree;
  nodes: TreeNode[];
  refs: NodeRef[][];
  p: number;
  onSeek: (index: number) => void;
  /** true while playback is actively advancing: only the step at the
   * playhead is lit and labelled. False (paused) lights the recent
   * ACTIVE_LEN window. */
  playing: boolean;
  /** A node click that means "scope to this" instead of "jump to
   * its next touch" (model nodes scope to that model, subagent nodes open
   * that subagent's own map). Return true when handled. */
  onNodeAction?: (node: TreeNode) => boolean;
  /** the nodes the dock's category selection lights (`mapFocus`); the rest
   * fade. Null: no category focus, the map renders as usual. */
  focus?: ReadonlySet<string> | null;
  /** the dock's category chips: also scopes the recent-touch window (dot
   * prominence + labels) to the selection, via `activeRefs` -- so it's the
   * selection's own last ACTIVE_LEN, not whatever else happened to be
   * touched most recently. Same set `focus` was built from. */
  selected?: ReadonlySet<Category>;
}

interface Tip {
  id: string;
  x: number;
  y: number;
}

function edgePath(n: TreeNode, root: TreeNode): string {
  const p = n.parent;
  if (!p) return '';
  if (p === root) return `M0 0 L${n.x} ${n.y}`;
  const rm = (p.r + n.r) / 2;
  const c1x = Math.cos(p.a) * rm;
  const c1y = Math.sin(p.a) * rm;
  const c2x = Math.cos(n.a) * rm;
  const c2y = Math.sin(n.a) * rm;
  return `M${p.x} ${p.y} C${c1x} ${c1y} ${c2x} ${c2y} ${n.x} ${n.y}`;
}

const isBig = (n: TreeNode) => n.type === 'repo' || n.type === 'group';
/** Nodes whose label runs along their spoke whenever it is shown. */
const hasSpokeLabel = (n: TreeNode) =>
  !n.children.length ||
  (n.type === 'dir' && n.depth <= 2) ||
  n.type === 'server' ||
  n.type === 'errsrc';
/** Where a node's spoke label starts, and how long it wants to be. */
const labelSpot = (n: TreeNode): LabelSpot => ({
  r0: n.r + (n.children.length ? 7 : 8),
  a: n.a,
  font: n.children.length ? BRANCH_FONT : LEAF_FONT,
  len: n.name.length,
});
/** Opacity multiplier for nodes outside the chip selection's focus. */
const FOCUS_DIM = 0.22;

interface DotStyle {
  fill: string;
  stroke: string;
  strokeWidth: number;
  r: number;
  opacity: number;
}

function dotStyle(
  n: TreeNode,
  s: NodeState | undefined,
  on: boolean,
  lit: boolean
): DotStyle {
  const big = isBig(n);
  if (LEAFISH.has(n.type)) {
    if (!s) {
      return {
        fill: HOLLOW_FILL,
        stroke: HOLLOW_RING,
        strokeWidth: 1.2,
        r: 2.6,
        opacity: 0.9,
      };
    }
    const color = n.type === 'file' ? TOUCH_COLOR[s.lvl] : nodeColor(n);
    return {
      fill: on ? color : FAINT_FILL,
      // a node that failed at the playhead wears a critical ring
      stroke: s.err ? ERROR_COLOR : on ? 'none' : color,
      strokeWidth: s.err ? 1.8 : 1,
      r: Math.min(3 + Math.sqrt(s.cnt) * 1.3, 9),
      opacity: on ? 1 : 0.7,
    };
  }
  return {
    fill: lit ? nodeColor(n) : HOLLOW_FILL,
    stroke: lit ? 'none' : HOLLOW_RING,
    strokeWidth: 1.2,
    r: big ? 5.5 : 3,
    opacity: lit ? 1 : 0.9,
  };
}

export function SessionMapSvg({
  map,
  tree,
  nodes,
  refs,
  p,
  onSeek,
  playing,
  onNodeAction,
  focus = null,
  // An omitted `selected` means "no narrowing", same as focus's own null
  // default.
  selected = ALL_CATEGORIES,
}: Props) {
  const [tip, setTip] = useState<Tip | null>(null);
  const hover = tip?.id ?? null;
  // Local, map-only "pinned leaf": clicking a visible leaf's label
  // toggles it. Does not touch ExplorerScope/URL/timeline/event chips.
  const [pinned, setPinned] = useState<string | null>(null);

  // Vertical room for the spoke labels near the top / bottom, per map.
  const padY = useMemo(
    () =>
      mapPadY(
        nodes
          .filter((n) => n.parent && !isBig(n) && hasSpokeLabel(n))
          .map(labelSpot)
      ),
    [nodes]
  );
  const fade = (id: string) => (focus && !focus.has(id) ? FOCUS_DIM : 1);
  // A narrowing chip selection also scopes what counts as "recently
  // touched": otherwise unselected activity (e.g. MCP calls while only
  // Edits is selected) fills the recent-N budget and an edited file's
  // label never appears, or stops tracking the playhead. `nextTouchOf`'s
  // seek target stays on the raw `refs` below -- scoping it would stop a
  // click from finding a real touch just because it's outside the current
  // selection.
  const focusRefs = useMemo(
    () => activeRefs(refs, map.events, selected),
    [refs, map.events, selected]
  );
  // `p` defaults to a "meaningful first frame" partway into the session
  // (initialPlayhead), not the end -- fine for the default view, but a
  // narrowing chip selection means "show me every match", not "only the
  // matches seen by wherever the scrubber happens to be sitting". So
  // while paused, a narrowed selection reads state as of the session's
  // last event instead of the live playhead; playing still animates the
  // reveal from `p` as before.
  const stP = !playing && narrowsView(selected) ? focusRefs.length - 1 : p;
  const st = useMemo(() => stateAt(focusRefs, stP), [focusRefs, stP]);
  // Also on stP, not the live p: `active` gates label visibility below
  // (line ~464) regardless of playing state, so a paused, narrowed view
  // needs it walking back from the full session too, or the dots light up
  // (via st, above) while their labels stay hidden -- still capped at
  // ACTIVE_LEN, same as ever, just anchored at the selection's own last
  // touch instead of wherever the scrubber happens to be.
  // The step at the playhead: the latest event at or before p that touches
  // a node in the selection. It pulses, its centre-to-leaf curve is drawn in
  // its colour, and -- for a subagent's step -- so is the path to that
  // subagent (in place of the old straight dashed link across the map).
  const touch = useMemo(() => lastTouch(focusRefs, p), [focusRefs, p]);
  const touchEvent = touch ? map.events[touch.i] : undefined;
  const agentId =
    touchEvent && touchEvent.agent && touchEvent.agent !== 'main'
      ? (tree.byId.get(`agent:${touchEvent.agent}`)?.id ?? null)
      : null;
  const touchPath = useMemo(
    () => (touch ? ancestorsOf(tree, new Set(touch.ids)) : null),
    [tree, touch]
  );
  const agentPath = useMemo(
    () => (agentId ? ancestorsOf(tree, new Set([agentId])) : null),
    [tree, agentId]
  );
  // What's lit and labelled. Playing: only that one step (and its
  // subagent) -- one leaf path at a time as the timeline moves, not a
  // recent-N window of leaves lighting up together. Paused: the recent
  // ACTIVE_LEN window from stP, plus the step itself so its pulse is never
  // an unlabelled dot.
  const active = useMemo(() => {
    const out = playing
      ? new Set<string>()
      : activeSet(focusRefs, stP, ACTIVE_LEN);
    for (const id of touch?.ids ?? []) out.add(id);
    if (agentId) out.add(agentId);
    return out;
  }, [playing, focusRefs, stP, touch, agentId]);
  const activeAnc = useMemo(() => ancestorsOf(tree, active), [tree, active]);
  const lit = useMemo(() => litSet(tree, st), [tree, st]);
  // Only active nodes render coloured (and their paths lit), paused or
  // playing: a node touched earlier but outside the window would keep a
  // coloured ring with no label. Coloured <=> labelled; everything else
  // reads as untouched.
  const windowedSt = useMemo(
    () => new Map([...st].filter(([id]) => active.has(id))),
    [st, active]
  );
  // activeAnc is already active's own ids plus their ancestors
  // (ancestorsOf includes the starting id), so it doubles as the windowed
  // lit set without a separate union.
  const windowedLit = activeAnc;
  // #4: pinning overrides the window -- the pinned leaf and its ancestor
  // chain always render at full prominence, even mid-playback and even out
  // of the recent window.
  const pinnedAnc = useMemo(
    () => (pinned ? ancestorsOf(tree, new Set([pinned])) : null),
    [tree, pinned]
  );
  const effectiveLit = useMemo(
    () => (pinnedAnc ? new Set([...windowedLit, ...pinnedAnc]) : windowedLit),
    [windowedLit, pinnedAnc]
  );

  const jumpTo = (id: string) => {
    const node = tree.byId.get(id);
    if (node && onNodeAction?.(node)) return;
    const i = nextTouchOf(tree, refs, p, id);
    if (i != null) onSeek(i);
  };

  const onMove = (ev: MouseEvent<SVGSVGElement>) => {
    const id = (ev.target as Element).getAttribute?.('data-id');
    if (!id) {
      if (tip) setTip(null);
      return;
    }
    const box = ev.currentTarget.getBoundingClientRect();
    setTip({ id, x: ev.clientX - box.left, y: ev.clientY - box.top });
  };

  const onClick = (ev: MouseEvent<SVGSVGElement>) => {
    const id = (ev.target as Element).getAttribute?.('data-id');
    if (id) jumpTo(id);
  };

  const onKey = (ev: KeyboardEvent<SVGCircleElement>, id: string) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      ev.stopPropagation();
      jumpTo(id);
    }
  };

  // A leaf's label click/Enter/Space toggles the pin (a second, independent
  // interactive target from its dot, which keeps seeking to next touch).
  const togglePin = (id: string) =>
    setPinned((cur) => (cur === id ? null : id));
  const onLabelKey = (ev: KeyboardEvent<SVGTextElement>, id: string) => {
    if (ev.key === 'Enter' || ev.key === ' ') {
      ev.preventDefault();
      ev.stopPropagation();
      togglePin(id);
    }
  };

  const pulseColor = touchEvent
    ? touchEvent.err
      ? ERROR_COLOR
      : kindStyle(touchEvent).color
    : TRAIL_COLOR;

  const tipNode = hover ? tree.byId.get(hover) : undefined;

  return (
    // Fills its pane (fit-viewport layout): the SVG takes the pane's full
    // height and scales the radial map into it (xMidYMid meet keeps it round
    // and centred; the dock's category chips carry the colour key). No CSS
    // transitions or animations here: playback re-renders it every tick.
    <div className="relative flex h-full min-h-0 flex-col">
      <svg
        viewBox={mapViewBox(padY)}
        preserveAspectRatio="xMidYMid meet"
        width="100%"
        height="100%"
        className="block min-h-0 w-full flex-1 select-none"
        role="img"
        aria-label="Session map: repositories, files, MCP tools, skills, subagents and errors touched in this session"
        onMouseMove={onMove}
        onMouseLeave={() => setTip(null)}
        onClick={onClick}
      >
        <g>
          {nodes.map((n) => {
            if (!n.parent) return null;
            const onPath = pinnedAnc?.has(n.id) ?? false;
            const dim = pinned != null && !onPath;
            // The current step's centre-to-leaf curve in its own colour, and
            // the acting subagent's path in the subagent colour.
            const stepColor = touchPath?.has(n.id)
              ? pulseColor
              : agentPath?.has(n.id)
                ? KIND_META.subagent.color
                : null;
            if (stepColor) {
              return (
                <path
                  key={`e-${n.id}`}
                  d={edgePath(n, tree.root)}
                  fill="none"
                  strokeWidth={2.2}
                  strokeLinecap="round"
                  stroke={stepColor}
                  opacity={(dim ? PIN_DIM : 1) * fade(n.id)}
                />
              );
            }
            return (
              <path
                key={`e-${n.id}`}
                d={edgePath(n, tree.root)}
                fill="none"
                strokeWidth={n.parent === tree.root ? 1.4 : 1}
                stroke={
                  effectiveLit.has(n.id)
                    ? 'var(--ds-border-default)'
                    : 'var(--ds-border-subtler)'
                }
                opacity={
                  (effectiveLit.has(n.id) ? 0.9 : 0.55) *
                  (dim ? PIN_DIM : 1) *
                  fade(n.id)
                }
              />
            );
          })}
        </g>
        <g>
          {nodes.map((n) => {
            if (n === tree.root) {
              return (
                <g key="root">
                  <circle r={9} fill="var(--ds-brand-subtlest)" />
                  <circle r={3.5} fill="var(--ds-brand-subtle)" />
                </g>
              );
            }
            const onPath = pinnedAnc?.has(n.id) ?? false;
            const d = dotStyle(
              n,
              onPath ? st.get(n.id) : windowedSt.get(n.id),
              active.has(n.id) || onPath,
              effectiveLit.has(n.id)
            );
            const leaf = !n.children.length;
            const dim = pinned != null && !onPath;
            return (
              <circle
                key={n.id}
                data-id={n.id}
                cx={n.x}
                cy={n.y}
                r={d.r}
                fill={d.fill}
                stroke={d.stroke}
                strokeWidth={d.strokeWidth}
                opacity={d.opacity * (dim ? PIN_DIM : 1) * fade(n.id)}
                tabIndex={leaf ? 0 : -1}
                aria-label={leaf ? `${n.name}, jump to next touch` : undefined}
                className="cursor-pointer outline-none focus-visible:stroke-ds-brand-subtle"
                onKeyDown={leaf ? (ev) => onKey(ev, n.id) : undefined}
              />
            );
          })}
        </g>
        <g pointerEvents="none">
          {nodes.map((n) => {
            if (n === tree.root) return null;
            const big = isBig(n);
            const show =
              big ||
              n.id === hover ||
              (pinnedAnc?.has(n.id) ?? false) ||
              (n.children.length ? activeAnc.has(n.id) : active.has(n.id)) ||
              (n.type === 'errsrc' && lit.has(n.id));
            if (!show) return null;
            const flip = Math.cos(n.a) < 0;
            if (big) {
              const lx = Math.cos(n.a) * (n.r + 14);
              const ly = Math.sin(n.a) * (n.r + 14);
              return (
                <text
                  key={`l-${n.id}`}
                  x={lx}
                  y={ly}
                  textAnchor={
                    Math.abs(Math.cos(n.a)) < 0.3
                      ? 'middle'
                      : flip
                        ? 'end'
                        : 'start'
                  }
                  dominantBaseline="middle"
                  fontSize={16}
                  fontWeight={600}
                  fill="var(--ds-text-darker)"
                  paintOrder="stroke"
                  stroke="var(--ds-surface-white)"
                  strokeWidth={4}
                  opacity={(lit.has(n.id) ? 1 : 0.45) * fade(n.id)}
                >
                  {n.name}
                </text>
              );
            }
            if (!hasSpokeLabel(n) && n.id !== hover) return null;
            const isLeaf = !n.children.length;
            const off = n.children.length ? 7 : 8;
            const lx = n.x + Math.cos(n.a) * off;
            const ly = n.y + Math.sin(n.a) * off;
            // The label runs along the node's own radial spoke, cut to what
            // fits above / below the circle (`labelChars`).
            const deg = (n.a * 180) / Math.PI;
            const rot = flip ? deg + 180 : deg;
            // A ×N suffix, not the old separate badge floating off to one
            // side of the dot (read as a prefix at some angles): it now
            // trails the name inside the same text run, so it always reads
            // after it, in the node's own colour to set it apart.
            const s = st.get(n.id);
            const showCount = COUNTED.has(n.type) && !!s && s.cnt > 1;
            const countTxt = s ? (s.cnt > 99 ? '99+' : String(s.cnt)) : '';
            const countColor = s
              ? n.type === 'file'
                ? TOUCH_COLOR[s.lvl]
                : nodeColor(n)
              : undefined;
            const budget = labelChars(labelSpot(n), padY);
            const nameBudget = showCount
              ? Math.max(3, budget - countTxt.length - 2)
              : budget;
            return (
              <text
                key={`l-${n.id}`}
                x={lx}
                y={ly}
                transform={`rotate(${rot} ${lx} ${ly})`}
                textAnchor={flip ? 'end' : 'start'}
                dominantBaseline="middle"
                fontSize={n.children.length ? BRANCH_FONT : LEAF_FONT}
                opacity={fade(n.id)}
                fill={
                  n.children.length
                    ? 'var(--ds-text-subtle)'
                    : 'var(--ds-text-default)'
                }
                fontFamily={MONO}
                paintOrder="stroke"
                stroke="var(--ds-surface-white)"
                strokeWidth={3}
                role={isLeaf ? 'button' : undefined}
                tabIndex={isLeaf ? 0 : undefined}
                aria-pressed={isLeaf ? n.id === pinned : undefined}
                style={
                  isLeaf
                    ? { cursor: 'pointer', pointerEvents: 'auto' }
                    : undefined
                }
                onClick={
                  isLeaf
                    ? (ev) => {
                        ev.stopPropagation();
                        togglePin(n.id);
                      }
                    : undefined
                }
                onKeyDown={isLeaf ? (ev) => onLabelKey(ev, n.id) : undefined}
              >
                {truncateLabel(n.name, nameBudget)}
                {showCount ? (
                  <tspan fill={countColor} fontWeight={700}>
                    {' '}
                    ×{countTxt}
                  </tspan>
                ) : null}
              </text>
            );
          })}
        </g>
        <g pointerEvents="none">
          {(touch?.ids ?? []).map((id) => {
            const n = tree.byId.get(id);
            if (!n) return null;
            return (
              <g key={`p-${id}`}>
                {/* One steady ring, no animation: during playback the
                    current node changes every tick, and a re-started
                    pulse read as the whole map flashing. */}
                <circle
                  cx={n.x}
                  cy={n.y}
                  r={8}
                  fill="none"
                  stroke={pulseColor}
                  strokeWidth={2}
                  opacity={0.55}
                />
                <circle cx={n.x} cy={n.y} r={4.5} fill={pulseColor} />
              </g>
            );
          })}
        </g>
      </svg>
      {tip && tipNode ? (
        <MapTooltip
          map={map}
          node={tipNode}
          state={st.get(tipNode.id)}
          x={tip.x}
          y={tip.y}
        />
      ) : null}
    </div>
  );
}

function MapTooltip({
  map,
  node,
  state,
  x,
  y,
}: {
  map: SessionMap;
  node: TreeNode;
  state: NodeState | undefined;
  x: number;
  y: number;
}) {
  const kids = node.children.length ? `${leafCount(node)} items below` : '';
  const fileId =
    node.type === 'file' ? Number(node.id.slice('file:'.length)) : null;
  // Last op seen for this file, a document-level
  // record so it stays correct even under an agent lens (the file itself
  // did not change scope, only which events are being replayed).
  const fileOp =
    fileId != null ? map.files.find((f) => f.id === fileId)?.op : undefined;
  const title =
    node.type === 'file'
      ? `${fileOp ? `${OP_GLYPH[fileOp]} ` : ''}${node.repoName}/${node.path}`
      : node.name;
  const touches = fileId != null ? touchesOfFile(map, fileId) : null;
  return (
    <div
      className="pointer-events-none absolute z-20 w-max max-w-[320px] rounded-lg border border-ds-border-subtlest bg-ds-surface-white p-2.5 shadow-md"
      style={{ left: Math.max(8, x + 12), top: y + 12 }}
      role="tooltip"
    >
      <div className="break-all ds-text-body-xs-semibold text-ds-text-darker">
        {title}
      </div>
      <div className="mt-0.5 ds-text-body-xs-medium text-ds-text-subtle">
        {TYPE_LABEL[node.type] ?? node.type}
        {kids ? ` · ${kids}` : ''}
      </div>
      {touches ? (
        <div className="mt-0.5 ds-text-body-xs-medium text-ds-text-default">
          session: {touches.read} reads · {touches.edit} edits · {touches.hit}{' '}
          hits
        </div>
      ) : null}
      <div className="mt-0.5 ds-text-body-xs-medium text-ds-text-subtle">
        {state
          ? `touched ${state.cnt}× by playhead · first at #${state.first}`
          : 'not yet touched at playhead'}
        {state?.err ? (
          <span className="text-ds-danger-default"> · {state.err} failed</span>
        ) : null}
        {node.children.length
          ? ''
          : node.type === 'model'
            ? ' · click to show only this model'
            : node.type === 'agent'
              ? " · click to open this subagent's map"
              : ' · click to jump to next touch'}
      </div>
    </div>
  );
}
