import * as React from 'react';
import { Popover as PopoverPrimitive } from '@base-ui/react/popover';

import { cn } from '@/lib/utils';

const Popover = PopoverPrimitive.Root;

const PopoverTrigger = PopoverPrimitive.Trigger;

const PopoverClose = PopoverPrimitive.Close;

const PopoverContent = React.forwardRef<
  React.ElementRef<typeof PopoverPrimitive.Popup>,
  React.ComponentPropsWithoutRef<typeof PopoverPrimitive.Popup> &
    Pick<
      PopoverPrimitive.Positioner.Props,
      'align' | 'alignOffset' | 'side' | 'sideOffset'
    >
>(
  (
    {
      className,
      align = 'center',
      alignOffset,
      side,
      sideOffset = 4,
      ...props
    },
    ref
  ) => (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Positioner
        className="isolate z-[1000]"
        align={align}
        alignOffset={alignOffset}
        side={side}
        sideOffset={sideOffset}
      >
        <PopoverPrimitive.Popup
          ref={ref}
          className={cn(
            'z-50 w-96 rounded-md border border-ds-border-subtler bg-ds-surface-white p-4 text-ds-text-default shadow-md outline-none transition-[opacity,transform] data-starting-style:opacity-0 data-ending-style:opacity-0 data-starting-style:scale-95 data-ending-style:scale-95 data-[side=bottom]:data-starting-style:-translate-y-2 data-[side=left]:data-starting-style:translate-x-2 data-[side=right]:data-starting-style:-translate-x-2 data-[side=top]:data-starting-style:translate-y-2',
            className
          )}
          {...props}
        />
      </PopoverPrimitive.Positioner>
    </PopoverPrimitive.Portal>
  )
);
PopoverContent.displayName = PopoverPrimitive.Popup.displayName;

export { Popover, PopoverTrigger, PopoverClose, PopoverContent };
