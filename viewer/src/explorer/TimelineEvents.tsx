import { useEffect, useRef } from 'react';
import { CaretLeft, CaretRight, X } from '@phosphor-icons/react';
import type { SessionMap, SessionMapEvent } from '@/core/schema';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { CATEGORY_COLOR, kindMeta, kindStyle } from '@/core/eventColors';
import {
  CATEGORIES,
  CATEGORY_LABEL,
  displayCategory,
  isDefaultCategories,
  stepPosition,
  type Category,
} from '@/core/sessionLayout';
import { clockAt, describeEvent } from '@/core/sessionMapModel';

const SINGULAR: Record<Category, string> = {
  prompt: 'prompt',
  error: 'error',
  delay: 'delay',
  out: 'output',
  edit: 'edit',
  explore: 'exploration call',
  mcp: 'MCP call',
  skill: 'skill call',
  tool: 'tool call',
  model: 'model request',
  cost: 'costed request',
  tokens: 'request',
};

const CHIP = 'inline-flex items-center rounded-full border py-px pl-px pr-0.5';
const chipTone = (on: boolean) =>
  on
    ? 'border-ds-border-default bg-ds-gray-subtlest'
    : 'border-ds-border-subtlest bg-ds-surface-white';
const CHIP_BUTTON =
  'inline-flex items-center gap-1.5 rounded-full py-0.5 pl-2 pr-1.5 text-xs font-medium';
const COUNT = 'font-mono text-[10.5px] text-ds-text-subtle tabular-nums';

/**
 * The dock's category chips: one per category with its count in the current
 * scope. A chip toggles the category in the selection (several may be on),
 * which lights its events on the timeline's Events bar, its nodes on the map
 * and its rows in Activity. A selected chip gets ‹ › to step to the previous
 * / next event of the category (pinning it), with "k/N" while one is pinned.
 * Reset returns to the default selection.
 */
export function CategoryChips({
  lists,
  selected,
  pinned,
  onToggle,
  onStep,
  onReset,
}: {
  lists: Record<Category, number[]>;
  selected: ReadonlySet<Category>;
  pinned: number | null;
  onToggle: (c: Category) => void;
  onStep: (c: Category, dir: 1 | -1) => void;
  onReset: () => void;
}) {
  return (
    // `contents`, not its own flex row: as a normal flex item this whole
    // group either fits next to PlaybackControls or, being wider than the
    // remaining space, wraps as one block -- stranding Playback alone on
    // its own line even when a few chips would have fit beside it. As
    // `contents` the chips become direct items of the dock's own row, so
    // they flow onto whichever line has room, same as everything else in
    // it. `role`/`aria-label` still apply: contents keeps element
    // semantics, it only removes the box.
    <div
      role="group"
      aria-label="Event categories: highlight on the timeline, map and activity"
      className="contents"
    >
      {CATEGORIES.map((c) => (
        <CategoryChip
          key={c}
          c={c}
          on={selected.has(c)}
          list={lists[c]}
          pinned={pinned}
          onToggle={onToggle}
          onStep={onStep}
        />
      ))}
      {isDefaultCategories(selected) ? null : (
        <Button
          variant="link"
          size="sm"
          onClick={onReset}
          title="Back to the default selection (Prompts and Errors)"
          className="h-6 px-1 text-ds-text-subtle"
        >
          Reset
        </Button>
      )}
    </div>
  );
}

function CategoryChip({
  c,
  on,
  list,
  pinned,
  onToggle,
  onStep,
}: {
  c: Category;
  on: boolean;
  list: readonly number[];
  pinned: number | null;
  onToggle: (c: Category) => void;
  onStep: (c: Category, dir: 1 | -1) => void;
}) {
  const at = on ? stepPosition(list, pinned) : null;
  return (
    <span className={cn(CHIP, chipTone(on), !on && 'pr-px')}>
      <button
        type="button"
        aria-pressed={on}
        onClick={() => onToggle(c)}
        title={`Highlight ${CATEGORY_LABEL[c]} on the timeline, map and activity`}
        className={cn(
          CHIP_BUTTON,
          on ? 'text-ds-text-darker' : 'text-ds-text-subtle'
        )}
      >
        <span
          aria-hidden
          className="size-2 shrink-0 rounded-sm"
          style={{ background: CATEGORY_COLOR[c] }}
        />
        {CATEGORY_LABEL[c]}
        <span className={COUNT}>{list.length.toLocaleString()}</span>
      </button>
      {on ? (
        <>
          <StepButton
            dir={-1}
            label={`Previous ${SINGULAR[c]}`}
            disabled={!list.length}
            onClick={() => onStep(c, -1)}
          />
          {at ? (
            <span className="font-mono text-[10.5px] text-ds-text-darker tabular-nums">
              {at}
            </span>
          ) : null}
          <StepButton
            dir={1}
            label={`Next ${SINGULAR[c]}`}
            disabled={!list.length}
            onClick={() => onStep(c, 1)}
          />
        </>
      ) : null}
    </span>
  );
}

/** An optional timeline lane's on/off chip (Context, Subagents): kept apart
 * from the category chips, since it adds a lane rather than lighting events. */
export function LaneToggle({
  label,
  color,
  count,
  on,
  onToggle,
}: {
  label: string;
  color: string;
  count?: number;
  on: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onToggle}
      title={`Show or hide the ${label} lane`}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border py-0.5 pl-2 pr-2.5 text-xs font-medium',
        chipTone(on),
        on ? 'text-ds-text-darker' : 'text-ds-text-subtle'
      )}
    >
      <span
        aria-hidden
        className="size-2 shrink-0 rounded-sm"
        style={{ background: color }}
      />
      {label}
      {count != null ? (
        <span className={COUNT}>{count.toLocaleString()}</span>
      ) : null}
    </button>
  );
}

function StepButton({
  dir,
  label,
  disabled,
  onClick,
}: {
  dir: 1 | -1;
  label: string;
  disabled: boolean;
  onClick: () => void;
}) {
  const Icon = dir < 0 ? CaretLeft : CaretRight;
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
      className="inline-flex size-5 items-center justify-center rounded text-ds-text-subtle hover:bg-ds-surface-white hover:text-ds-text-darker disabled:opacity-35"
    >
      <Icon size={11} weight="bold" />
    </button>
  );
}

/** One line of the events list: what happened, in a few words. */
function eventRowLabel(
  map: SessionMap,
  e: SessionMapEvent,
  ordinals: Map<string, number>
): string {
  if (e.kind === 'user') return `Prompt · ${e.chars ?? 0} chars`;
  const d = describeEvent(map, e, ordinals).title;
  if (!e.err) return d;
  const k = kindStyle(e).label;
  return e.kind === 'error'
    ? `${k} · ${d}`
    : `Failed ${k.toLowerCase()} · ${d}`;
}

const markColor = (e: SessionMapEvent, c: Category) =>
  c === 'out'
    ? kindMeta(e.vcs?.action === 'pr_create' ? 'pr_create' : e.kind).color
    : CATEGORY_COLOR[c];

/**
 * The events list, opened from the dock's "List" button: every event in the
 * selected categories, in time order. It floats above the dock (over the map)
 * rather than pushing the layout; Escape or × closes it. Clicking a row pins
 * the event; hovering one highlights its mark on the timeline.
 */
export function EventsPanel({
  map,
  rows,
  selected,
  ordinals,
  pinned,
  onPin,
  onHover,
  onClose,
}: {
  map: SessionMap;
  rows: readonly number[];
  /** the dock's selected categories (each row's swatch follows them) */
  selected: ReadonlySet<Category>;
  ordinals: Map<string, number>;
  pinned: number | null;
  onPin: (i: number) => void;
  onHover: (i: number | null) => void;
  onClose: () => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  // Bring the pinned row into view when the list opens or the pin moves.
  useEffect(() => {
    const box = boxRef.current;
    const row = box?.querySelector<HTMLElement>('[aria-selected="true"]');
    if (box && row) box.scrollTop = Math.max(0, row.offsetTop - 60);
  }, [pinned]);
  const startedAt = map.session.startedAt;
  return (
    <div className="absolute bottom-full right-3 z-30 mb-1.5 flex max-h-[340px] w-[min(560px,90%)] flex-col rounded-lg border border-ds-border-default bg-ds-surface-white shadow-lg">
      <div className="flex items-center gap-2 border-b border-ds-border-subtlest px-3 py-2">
        <span className="ds-text-body-xs-semibold text-ds-text-darker">
          {rows.length.toLocaleString()} events
        </span>
        <span className="ds-text-body-xs-medium text-ds-text-subtle">
          categories follow the chips · click to pin on the timeline
        </span>
        <span className="flex-1" />
        <button
          type="button"
          onClick={onClose}
          aria-label="Close the events list (Escape)"
          className="inline-flex size-6 items-center justify-center rounded text-ds-text-subtle hover:bg-ds-gray-subtlest hover:text-ds-text-darker"
        >
          <X size={14} />
        </button>
      </div>
      <div
        ref={boxRef}
        role="listbox"
        aria-label="Session events"
        className="relative min-h-0 flex-1 overflow-y-auto overscroll-contain"
        onMouseLeave={() => onHover(null)}
      >
        {rows.length ? (
          rows.map((i) => {
            const e = map.events[i];
            const c = displayCategory(e, selected);
            const sel = pinned === i;
            return (
              <button
                key={i}
                type="button"
                role="option"
                aria-selected={sel}
                onClick={() => onPin(i)}
                onMouseEnter={() => onHover(i)}
                onFocus={() => onHover(i)}
                className={cn(
                  'grid w-full grid-cols-[44px_10px_minmax(0,1fr)_40px_64px] items-center gap-2 border-b border-ds-border-subtlest px-3 py-1 text-left text-xs hover:bg-ds-gray-subtlest',
                  sel && 'bg-ds-brand-lightest'
                )}
              >
                <span className="font-mono text-[11px] text-ds-text-subtle">
                  {clockAt(startedAt, e.t).slice(0, 5)}
                </span>
                <span
                  aria-hidden
                  className={cn(
                    'size-2 rounded-sm',
                    c === 'out' && 'rotate-45 scale-90'
                  )}
                  style={{ background: markColor(e, c) }}
                />
                <span className="truncate text-ds-text-default">
                  {eventRowLabel(map, e, ordinals)}
                </span>
                <span className="truncate text-[11px] text-ds-text-subtle">
                  {typeof e.task === 'number' ? `T${e.task}` : ''}
                </span>
                <span className="truncate text-[11px] text-ds-text-subtle">
                  {e.agent === 'main'
                    ? 'main'
                    : `Sub ${ordinals.get(e.agent ?? '') ?? ''}`}
                </span>
              </button>
            );
          })
        ) : (
          <p className="px-3 py-4 ds-text-body-xs-medium text-ds-text-subtle">
            No events in the selected categories in this scope. Switch a chip
            on.
          </p>
        )}
      </div>
    </div>
  );
}
