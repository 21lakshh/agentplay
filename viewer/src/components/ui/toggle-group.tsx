import * as React from 'react';
import { ToggleGroup as ToggleGroupPrimitive } from '@base-ui/react/toggle-group';
import { Toggle as TogglePrimitive } from '@base-ui/react/toggle';

import { cn } from '@/lib/utils';

const ToggleGroup = React.forwardRef<
  HTMLDivElement,
  React.ComponentProps<typeof ToggleGroupPrimitive>
>(({ className, ...props }, ref) => (
  <ToggleGroupPrimitive
    ref={ref}
    className={cn(
      'inline-flex items-center gap-1 rounded-md border border-ds-border-subtlest bg-ds-surface-white p-0.5',
      className
    )}
    {...props}
  />
));
ToggleGroup.displayName = 'ToggleGroup';

const ToggleGroupItem = React.forwardRef<
  HTMLButtonElement,
  React.ComponentProps<typeof TogglePrimitive>
>(({ className, children, ...props }, ref) => (
  <TogglePrimitive
    ref={ref}
    className={cn(
      'inline-flex items-center gap-1.5 rounded px-3 py-1 ds-text-body-xs-medium text-ds-text-subtle transition-colors hover:text-ds-text-darker data-[pressed]:bg-ds-gray-subtlest data-[pressed]:text-ds-text-darker',
      className
    )}
    {...props}
  >
    {children}
  </TogglePrimitive>
));
ToggleGroupItem.displayName = 'ToggleGroupItem';

export { ToggleGroup, ToggleGroupItem };
