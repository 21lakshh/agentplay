import type { SessionMap } from '@/core/schema';
import { cn } from '@/lib/utils';
import { kindStyle } from '@/core/eventColors';
import { clockAt, describeEvent } from '@/core/sessionMapModel';
import { detailRows } from '@/core/sessionSignals';

/** The hover card of a timeline mark (drawn outside the lane scrollport). */
export function TimelineTooltip({
  map,
  i,
  cumCost,
  ordinals,
  left,
  top,
}: {
  map: SessionMap;
  i: number;
  cumCost: Float64Array;
  ordinals: Map<string, number>;
  left: number;
  top: number;
}) {
  const ev = map.events[i];
  const K = kindStyle(ev);
  const D = describeEvent(map, ev, ordinals);
  const rows = detailRows(map, i, cumCost, ordinals, true);
  return (
    <div
      role="tooltip"
      className="pointer-events-none absolute z-20 w-[252px] rounded-lg border border-ds-border-subtlest bg-ds-surface-white p-2.5 shadow-md"
      style={{ left, top, transform: 'translateY(calc(-100% - 10px))' }}
    >
      <div className="flex items-center gap-1.5 ds-text-body-xs-semibold text-ds-text-darker">
        <span
          aria-hidden
          className="size-2 shrink-0 rounded-full"
          style={{ background: K.color }}
        />
        <span className="truncate">{D.title}</span>
        <span className="ml-auto shrink-0 font-mono ds-text-body-xs-medium text-ds-text-subtle">
          #{i} · {clockAt(map.session.startedAt, ev.t)}
        </span>
      </div>
      {ev.kind !== 'llm' && D.detail ? (
        <div className="mt-1 break-all ds-text-body-xs-medium text-ds-text-subtle">
          {D.detail}
        </div>
      ) : null}
      {rows.length ? (
        <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 ds-text-body-xs-medium">
          {rows.map((r) => (
            <div key={r.k} className="contents">
              <dt className="text-ds-text-subtle">{r.k}</dt>
              <dd
                className={cn(
                  'text-right font-mono',
                  r.tone === 'bad'
                    ? 'text-ds-danger-default'
                    : r.tone === 'warn'
                      ? 'text-ds-warning-default'
                      : 'text-ds-text-darker'
                )}
              >
                {r.v}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
