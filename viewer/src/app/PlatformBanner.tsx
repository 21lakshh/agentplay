import { parseAsString, useQueryState } from 'nuqs';
import { ArrowRight } from '@phosphor-icons/react';
import { connectUrl, researchUrl } from '@/core/entelligence';
import { useSampleMode } from '@/app/sampleMode';

const link =
  'whitespace-nowrap text-ds-brand-default underline underline-offset-2 hover:text-ds-text-darker';

/**
 * A standing line above the explorer: why agentplay exists, and the three ways
 * further in. It stays put on purpose -- it is the one place every view links
 * the write-up, the review demo and the product, without interrupting anyone.
 *
 * The demo link swaps onto the bundled sample (as "See sample" does) and opens
 * the right column's Review tab, where that sample's review comments are.
 * Both are URL params set in one update, so Back undoes the whole jump.
 */
export function PlatformBanner() {
  const sample = useSampleMode();
  const [, setPanel] = useQueryState('panel', parseAsString);
  const openDemo = () => {
    sample.show();
    void setPanel('review');
  };
  return (
    <div
      role="note"
      aria-label="About agentplay"
      className="flex shrink-0 flex-wrap items-center justify-center gap-x-3 gap-y-1 rounded-lg border border-ds-brand-subtlest bg-ds-brand-lightest px-3 py-1.5 text-center ds-text-body-xs-medium text-ds-text-default"
    >
      <span>agentplay is the first step toward coding agents that learn from their own mistakes.</span>
      <a href={researchUrl('banner')} target="_blank" rel="noopener noreferrer" className={link}>
        Read the whitepaper
      </a>
      <span aria-hidden className="text-ds-text-subtler">
        ·
      </span>
      {sample.active && sample.canExit ? (
        <button type="button" onClick={sample.exit} className={link}>
          Back to your sessions
        </button>
      ) : (
        <button type="button" onClick={openDemo} disabled={sample.active} className={link}>
          See it on a sample PR
        </button>
      )}
      <span aria-hidden className="text-ds-text-subtler">
        ·
      </span>
      <a
        href={connectUrl('banner')}
        target="_blank"
        rel="noopener noreferrer"
        className={`${link} inline-flex items-center gap-1`}
      >
        Entelligence for teams
        <ArrowRight size={12} aria-hidden />
      </a>
    </div>
  );
}
