import type React from 'react';
import { cn } from '@/lib/utils';

interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  shimmer?: boolean;
}

export function Skeleton({
  className,
  shimmer = true,
  ...props
}: SkeletonProps) {
  return (
    <div
      className={cn(
        'rounded-md bg-muted',
        // Travelling sheen rather than an opacity pulse: a pulse reads as
        // "something is blinking", a sheen reads as "data is arriving". Shared
        // with the widget placeholders (index.css `widget-skeleton-*`) so
        // every loading surface in the product moves the same way.
        shimmer && 'widget-skeleton-sheen',
        className
      )}
      {...props}
    />
  );
}
