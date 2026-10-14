import { useState } from 'react';
import { ArrowSquareOut } from '@phosphor-icons/react';
import { buttonVariants } from '@/components/ui/button';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { connectUrl } from '@/core/entelligence';
import { cn } from '@/lib/utils';
import { DELIVERIES, type DeliveryId, type SampleComment } from './reviewSample';

/**
 * What "Fix now" on a review comment opens: the rule the comment turns into,
 * and the four build-time routes it can reach the agent by.
 *
 * The viewer installs nothing. Every route here happens when a session is
 * built, on a machine with the repo and an account, which is exactly why the
 * block around it is locked -- so the dialog says so rather than offering a
 * button that would quietly do nothing.
 */
export function FixNowDialog({ c }: { c: SampleComment }) {
  const [delivery, setDelivery] = useState<DeliveryId>(c.fix.delivery);
  return (
    <Dialog>
      <DialogTrigger
        render={
          <button
            type="button"
            className={cn(
              buttonVariants({ variant: 'secondary', size: 'sm' }),
              'cursor-pointer'
            )}
          >
            Fix now
          </button>
        }
      />
      <DialogContent aria-describedby={undefined}>
        <div className="flex flex-col gap-1">
          <DialogTitle className="ds-text-body-sm-semibold text-ds-text-darker">
            Fix {c.flagged.toLowerCase()}
          </DialogTitle>
          <DialogDescription className="font-mono ds-text-body-xs-medium text-ds-text-subtle">
            {c.file} {c.line} · edit #{c.editSeq} · {c.learning}
          </DialogDescription>
        </div>

        <section className="flex flex-col gap-1 rounded-lg border border-ds-border-subtlest bg-ds-gray-subtlest p-3">
          <h3 className="ds-text-body-xs-semibold text-ds-text-subtle">
            The rule
          </h3>
          <p className="ds-text-body-xs-medium text-ds-text-default">
            {c.fix.rule}
          </p>
          <p className="ds-text-body-xs-medium text-ds-text-subtle">
            Fires: {c.fix.trigger}
          </p>
        </section>

        <section className="flex flex-col gap-2">
          <h3 className="ds-text-body-xs-semibold text-ds-text-subtle">
            Delivery at build time
          </h3>
          <div role="radiogroup" aria-label="Delivery at build time" className="flex flex-col gap-1.5">
            {DELIVERIES.map((d) => (
              <button
                key={d.id}
                type="button"
                role="radio"
                aria-checked={delivery === d.id}
                onClick={() => setDelivery(d.id)}
                className={cn(
                  'flex cursor-pointer flex-col gap-0.5 rounded-lg border p-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ds-brand-subtler',
                  delivery === d.id
                    ? 'border-ds-brand-subtler bg-ds-brand-lightest'
                    : 'border-ds-border-subtlest hover:bg-ds-gray-subtlest'
                )}
              >
                <span className="ds-text-body-xs-semibold text-ds-text-darker">
                  {d.label}
                  {d.id === c.fix.delivery ? (
                    <span className="ml-1.5 ds-text-body-xs-medium text-ds-brand-default">
                      suggested
                    </span>
                  ) : null}
                </span>
                <span className="ds-text-body-xs-medium text-ds-text-subtle">
                  {d.detail}
                </span>
              </button>
            ))}
          </div>
        </section>

        <p className="ds-text-body-xs-medium text-ds-text-subtle">
          Nothing is written from here. Entelligence compiles the rule in when
          this repo&rsquo;s next session is built, records which sessions got
          it, and holds a share back so the before/after is measured against a
          control rather than against memory.
        </p>

        <div className="flex flex-wrap items-center gap-2">
          <a
            href={connectUrl('fix-dialog')}
            target="_blank"
            rel="noopener noreferrer"
            className={cn(
              buttonVariants({ variant: 'default', size: 'sm' }),
              'border-transparent bg-ds-brand-fill font-semibold text-ds-brand-on-fill hover:bg-ds-brand-subtler active:bg-ds-brand-subtler',
              'shadow-[0_0_0_1px_var(--primitive-brand-green-500),0_3px_16px_-2px_var(--primitive-brand-green-400)] transition-shadow hover:shadow-[0_0_0_1px_var(--primitive-brand-green-400),0_5px_24px_-2px_var(--primitive-brand-green-400)]'
            )}
          >
            Connect your PRs
            <ArrowSquareOut size={12} aria-hidden />
          </a>
          <DialogClose
            render={
              <button
                type="button"
                className={cn(
                  buttonVariants({ variant: 'ghost', size: 'sm' }),
                  'cursor-pointer'
                )}
              >
                Close
              </button>
            }
          />
        </div>
      </DialogContent>
    </Dialog>
  );
}
