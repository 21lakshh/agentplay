import * as React from 'react';
import { cva, type VariantProps } from 'class-variance-authority';

import { cn } from '@/lib/utils';

/**
 * Every fill/ink pair below clears 4.5:1 in BOTH themes, checked by
 * a contrast audit. Five of the nine variants did not
 * when that check was first written: `secondary` 3.86:1, `destructive` 3.61:1,
 * `keyline` 3.95:1, `success` 3.18:1 and `warning` 3.27:1, all in LIGHT mode,
 * all shipped. Every one had been eyeballed and passed, because a 3.2:1 chip
 * looks fine next to a 9:1 one until you compute it.
 *
 * If you add or retune a variant, re-check contrast in both themes.
 */
const badgeVariants = cva(
  'inline-flex items-center gap-1 rounded-lg border border-transparent px-2 py-0.5 ds-text-body-xs-medium shadow-none transition-colors focus:outline-none focus:ring-2 focus:ring-ds-brand-subtle focus:ring-offset-2',
  {
    variants: {
      variant: {
        // Ink is `subtle`, not `default`: every consumer was already
        // overriding it to `text-ds-brand-subtle`. Still AA on the subtlest
        // fill: 5.4:1 light, 9.0:1 dark.
        default: 'bg-ds-brand-subtlest text-ds-brand-subtle',
        secondary: 'bg-ds-gray-subtlest text-ds-text-default',
        destructive: 'bg-ds-danger-subtlest text-ds-danger-darkest',
        outline: 'border-ds-border-subtlest text-ds-text-default',
        info: 'bg-ds-purple-subtlest text-ds-purple-default',
        // Defines the variant; it does not choose it. Product code wanting a
        // positive metric wants `default` (brand color), not this.
        // Uses `-subtlest` like its siblings, so it never reads as a solid
        // button-like fill in dark mode.
        success: 'bg-ds-success-subtlest text-ds-success-darker',
        warning: 'bg-ds-warning-subtlest text-ds-warning-darker',
        // The two brand weights above a tint, so a surface that needs to rank
        // three states (filled > ringed > tinted > neutral) can do it through
        // the design system instead of a private class map. Weight carries the
        // hierarchy; the hue never changes.
        brandSolid: 'bg-ds-brand-fill text-ds-brand-on-fill',
        // The quiet annotation chip: "12 engineers", "spend-weighted", "Low
        // sample", "approx". Lower emphasis than `secondary`, and bordered so it
        // reads as a qualifier on the number beside it rather than a status of
        // its own. Six files had hand-rolled this exact look before it was a
        // variant.
        keyline:
          'border-ds-border-subtlest bg-ds-gray-lightest text-ds-text-default',
        brandOutline:
          'border-ds-brand-subtler bg-ds-brand-subtlest text-ds-brand-subtle',
        // Alias of `destructive`, kept because call sites reach for whichever
        // word fits the sentence ("closed" is an error, "delete" is
        // destructive). Same tokens on purpose -- do not let them drift, and do
        // not add a third.
        error: 'bg-ds-danger-subtlest text-ds-danger-darkest',
      },
    },
    defaultVariants: {
      variant: 'default',
    },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return (
    <div className={cn(badgeVariants({ variant }), className)} {...props} />
  );
}

export { Badge, badgeVariants };
