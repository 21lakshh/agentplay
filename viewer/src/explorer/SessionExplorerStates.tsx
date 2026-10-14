import { ArrowClockwise, WarningCircle } from '@phosphor-icons/react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';

const CARD = 'rounded-xl border border-ds-border-subtlest bg-ds-surface-white';
const FILL = 'bg-ds-gray-subtlest';

/** Main column placeholder: header, map card, timeline card. */
export function MapSkeleton() {
  return (
    <div className="flex min-w-0 flex-col gap-4" aria-hidden>
      <div className="flex flex-col gap-2">
        <Skeleton className={`h-5 w-2/3 ${FILL}`} />
        <Skeleton className={`h-3.5 w-1/2 ${FILL}`} />
      </div>
      <div className={`${CARD} flex items-center justify-center p-4`}>
        <Skeleton className={`size-[440px] rounded-full ${FILL}`} />
      </div>
      <div className={`${CARD} flex flex-col gap-2 p-4`}>
        <Skeleton className={`h-14 w-full ${FILL}`} />
        <Skeleton className={`h-9 w-full ${FILL}`} />
        <Skeleton className={`h-7 w-full ${FILL}`} />
        <Skeleton className={`h-8 w-1/2 ${FILL}`} />
      </div>
    </div>
  );
}

export function SidePanelsSkeleton() {
  return (
    <div className="flex flex-col gap-3" aria-hidden>
      {[88, 120, 260, 96, 150].map((h) => (
        <div key={h} className={`${CARD} p-3`}>
          <Skeleton className={`mb-2 h-3.5 w-1/3 ${FILL}`} />
          <Skeleton className={`w-full ${FILL}`} style={{ height: h - 40 }} />
        </div>
      ))}
    </div>
  );
}

function ListSkeleton() {
  return (
    <div className={`${CARD} flex flex-col gap-2 p-3`} aria-hidden>
      <Skeleton className={`h-8 w-full ${FILL}`} />
      <Skeleton className={`h-3 w-1/2 ${FILL}`} />
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="flex flex-col gap-1.5 py-1.5">
          <Skeleton className={`h-4 w-5/6 ${FILL}`} />
          <Skeleton className={`h-3 w-2/3 ${FILL}`} />
          <Skeleton className={`h-4 w-1/2 ${FILL}`} />
        </div>
      ))}
    </div>
  );
}

/** Whole-explorer placeholder shaped like list | main | side. */
export function SessionExplorerSkeleton() {
  return (
    <div
      className="grid grid-cols-[280px_minmax(0,1fr)_320px] gap-4"
      role="status"
      aria-label="Loading Session Explorer"
    >
      <ListSkeleton />
      <MapSkeleton />
      <SidePanelsSkeleton />
    </div>
  );
}

export function InlineError({
  title,
  description,
  onRetry,
}: {
  title: string;
  description: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className={`${CARD} flex min-h-[220px] flex-col items-center justify-center gap-3 p-6 text-center`}
    >
      <div className="flex size-10 items-center justify-center rounded-lg bg-ds-danger-subtlest">
        <WarningCircle
          size={22}
          weight="duotone"
          className="text-ds-danger-default"
        />
      </div>
      <div className="flex max-w-[360px] flex-col gap-1">
        <span className="ds-text-body-xs-semibold text-ds-text-darker">
          {title}
        </span>
        <span className="ds-text-body-xs-medium text-ds-text-subtle">
          {description}
        </span>
      </div>
      {onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry}>
          <ArrowClockwise size={14} />
          Try again
        </Button>
      ) : null}
    </div>
  );
}
