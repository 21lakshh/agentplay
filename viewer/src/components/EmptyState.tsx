import { type ReactNode } from 'react';
import { Tray } from '@phosphor-icons/react';

import { cn } from '@/lib/utils';

/**
 * In-card empty state: icon in brand box, title, subtext.
 * Use for "No data", "No patterns yet", "No repositories match", etc.
 */
interface EmptyStateProps {
  /** Main heading (e.g. "No activity in this period") */
  title: string;
  /** Supporting line (e.g. "Try adjusting the date range...") */
  description: string;
  /** Optional icon; default is Tray for generic empty */
  icon?: ReactNode;
  /** Optional CTA (e.g. a Button) rendered under the description */
  action?: ReactNode;
  /**
   * Vertical footprint. `md` (default, ~230px) is the standalone/section form
   * used when the empty state IS the widget body. `sm` (~130px) is for a card
   * sitting in a grid beside siblings that carry data -- e.g.
   * sidebar cards, where the full form would out-tall the real content it
   * replaces. Same anatomy either way, so a row of mixed states still reads as
   * one system.
   */
  size?: 'sm' | 'md';
  /** Layout-only overrides (e.g. a min-height matched to the widget body). */
  className?: string;
}

export function EmptyState({
  title,
  description,
  icon,
  action,
  size = 'md',
  className,
}: EmptyStateProps) {
  const sm = size === 'sm';
  return (
    <div
      className={cn(
        'flex w-full flex-col items-center justify-center text-center',
        sm ? 'min-h-[130px] gap-3 p-4' : 'min-h-[180px] gap-6 p-6',
        className
      )}
      role="status"
      aria-label={title}
    >
      <div
        className={cn(
          'flex shrink-0 items-center justify-center rounded-lg border border-ds-brand-subtlest bg-ds-brand-lightest/20',
          sm ? 'size-8' : 'size-10'
        )}
        aria-hidden
      >
        {icon ?? (
          <Tray
            size={sm ? 18 : 24}
            className="text-ds-brand-subtler"
            weight="regular"
            aria-hidden
          />
        )}
      </div>
      <div className="flex w-full flex-col items-center gap-2 text-center">
        <p className="ds-text-body-sm-semibold text-ds-text-default w-full">
          {title}
        </p>
        <p
          className={cn(
            'ds-text-body-xs-medium text-ds-text-subtle w-full',
            sm ? 'max-w-[320px]' : 'max-w-[280px]'
          )}
        >
          {description}
        </p>
        {action && (
          <div className="mt-2 flex items-center justify-center">{action}</div>
        )}
      </div>
    </div>
  );
}
