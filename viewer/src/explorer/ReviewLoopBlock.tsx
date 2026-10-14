import { ArrowRight } from '@phosphor-icons/react';
import { parseAsString, useQueryState } from 'nuqs';
import { Badge } from '@/components/ui/badge';
import { buttonVariants } from '@/components/ui/button';
import { LockedBlock } from '@/components/LockedBlock';
import { FixNowDialog } from './FixNowDialog';
import { SAMPLE_SESSION_ID, useSampleMode } from '@/app/sampleMode';
import { reviewLoopCopy, type LinkSource } from '@/core/entelligence';
import {
  SAMPLE_COMMENTS,
  type SampleComment as SampleCommentData,
} from './reviewSample';
import type { SessionMap } from '@/core/schema';
import { srcSeqOf } from '@/core/sessionMapModel';
import { cn } from '@/lib/utils';

/**
 * The review loop, in the outcomes panel: a review comment on a PR, the edit
 * that caused it, and the rule the repeats cluster into.
 *
 * It gates a capability the map cannot have rather than a feature that works
 * offline. Review comment text needs the host's API and an authenticated
 * account; attributing one to an edit needs the comment plus the session; the
 * cluster needs every other session too. None of that can be computed from a
 * local document, so nothing here is being withheld.
 */
export function ReviewLoopBlock({
  map,
  src = 'outcomes',
  path = null,
  onPath,
  onSeek,
}: {
  map: SessionMap;
  src?: LinkSource;
  /** the picked comment's causal path, lit on the map */
  path?: readonly number[] | null;
  onPath?: (seqs: readonly number[] | null) => void;
  /** move the playhead to the flagged edit when a comment is picked */
  onSeek?: (index: number) => void;
}) {
  const copy = reviewLoopCopy(map.prs);
  const [sample, setSample] = useQueryState('sample', parseAsString);
  const mode = useSampleMode();
  // Revealed only on the session the comments are about. `sample` survives
  // picking another session out of the sidebar, and the steps they cite
  // exist in that map too -- they would light the wrong nodes in it.
  const shown = sample !== null && map.session.id === SAMPLE_SESSION_ID;
  return (
    <LockedBlock
      title={copy.title}
      blurb={copy.blurb}
      ctaLabel="Connect your PRs"
      src={src}
      preview={
        <ReviewLoopSample
          path={path}
          onPath={onPath}
          onSeek={
            onSeek
              ? (seq) => {
                  // `srcSeqOf`, not `e.seq`: under a task or model scope the
                  // map is a renumbered copy, so the cited number read
                  // against `seq` parks the playhead on an unrelated event.
                  const i = map.events.findIndex((e) => srcSeqOf(e) === seq);
                  if (i >= 0) onSeek(i);
                }
              : undefined
          }
        />
      }
      shown={shown}
      // Showing the sample swaps the whole view onto the bundled session
      // that opened a PR, so the map, the timeline and the panels are all
      // the same session the comment below belongs to. Hiding it only
      // re-blurs: leaving the sample is a separate, named action, because
      // doing it silently would throw away a session the user had open.
      onToggle={() => (shown ? void setSample(null) : mode.show())}
      exit={
        mode.active && mode.canExit ? (
          <button
            type="button"
            onClick={mode.exit}
            className={cn(buttonVariants({ variant: 'link', size: 'sm' }))}
          >
            Back to your sessions
          </button>
        ) : null
      }
    />
  );
}

/**
 * Invented, and labelled as such by `LockedBlock` -- but every step number,
 * file and task below is a real one from the demo session the "See sample"
 * button switches to, so a reader can scrub the timeline and find each one.
 * Shaped like the real panel: comment, cause, causal path, cluster.
 */
function ReviewLoopSample({
  path,
  onPath,
  onSeek,
}: {
  path?: readonly number[] | null;
  onPath?: (seqs: readonly number[] | null) => void;
  onSeek?: (seq: number) => void;
}) {
  const sameAs = (c: SampleCommentData) =>
    !!path && path.length === c.path.length && path.every((s, i) => s === c.path[i].seq);
  return (
    <div className="flex flex-col gap-3">
      <p className="ds-text-body-xs-semibold text-ds-text-subtle">
        {SAMPLE_COMMENTS.length} review comments on acme/api #142
        {onPath ? ' \u00b7 pick one to light its path on the map' : null}
      </p>
      {SAMPLE_COMMENTS.map((c) => (
        <SampleComment
          key={c.editSeq}
          c={c}
          picked={sameAs(c)}
          onPick={
            onPath
              ? () => {
                  const clearing = sameAs(c);
                  onPath(clearing ? null : c.path.map((s) => s.seq));
                  // Park the playhead on the flagged edit, so the timeline
                  // and the Event panel agree with the lit map.
                  if (!clearing) onSeek?.(c.editSeq);
                }
              : undefined
          }
        />
      ))}
    </div>
  );
}

/**
 * One comment. Picking it lights its causal path on the map and dims the
 * rest -- the same focus mechanism the dock's category chips use, so the
 * path is drawn by the map rather than painted over it. Picking it again
 * clears the focus.
 */
function SampleComment({
  c,
  picked,
  onPick,
}: {
  c: SampleCommentData;
  picked: boolean;
  onPick?: () => void;
}) {
  const body = (
    <>
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate font-mono ds-text-body-xs-medium text-ds-danger-default">
          {c.file} {c.line}
        </span>
        <span className="shrink-0 ds-text-body-xs-medium text-ds-text-subtle">
          @{c.author}
        </span>
      </div>
      <p className="ds-text-body-xs-medium text-ds-text-default">“{c.text}”</p>
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant="error">{c.flagged}</Badge>
        <span className="font-mono ds-text-body-xs-medium text-ds-text-subtle">
          edit #{c.editSeq}
        </span>
      </div>
      <p className="ds-text-body-xs-medium text-ds-text-subtle">
        Task: {c.task}
      </p>
      <div className="flex flex-col gap-0.5">
        <span className="ds-text-body-xs-semibold text-ds-text-subtle">
          Cause · {c.cause}
        </span>
        <span className="ds-text-body-xs-medium text-ds-text-default">
          {c.why}
        </span>
      </div>
      <div className="flex flex-col gap-0.5">
        <span className="ds-text-body-xs-semibold text-ds-text-subtle">
          Causal path
        </span>
        {c.path.map((s) => (
          <div key={s.seq} className="flex items-baseline gap-2">
            <span className="w-8 shrink-0 font-mono ds-text-body-xs-medium text-ds-text-subtle">
              #{s.seq}
            </span>
            <span className="w-8 shrink-0 ds-text-body-xs-medium text-ds-text-subtle">
              {s.who}
            </span>
            <span className="min-w-0 flex-1 font-mono ds-text-body-xs-medium text-ds-text-default">
              {s.what}
            </span>
          </div>
        ))}
      </div>
      {c.ghost ? (
        <p className="ds-text-body-xs-medium text-ds-text-subtle">
          Ghost: {c.ghost}
        </p>
      ) : null}
      <div className="flex items-center gap-1 ds-text-body-xs-medium text-ds-brand-default">
        <span>
          Clustered into {c.learning} with {c.alsoIn}{' '}
          {c.alsoIn === 1 ? 'other session' : 'other sessions'}
        </span>
        <ArrowRight size={12} aria-hidden className="shrink-0" />
      </div>
    </>
  );
  // The card is a div, not a button: "Fix now" lives inside it, and a
  // button inside a button is invalid and unreachable by keyboard. The
  // comment itself is the pick target; the footer is its own row.
  return (
    <div
      className={cn(
        'flex w-full flex-col gap-1.5 rounded-md border p-2 text-left transition-colors',
        picked
          ? 'border-ds-brand-subtler bg-ds-brand-lightest'
          : 'border-ds-border-subtlest'
      )}
    >
      {onPick ? (
        <button
          type="button"
          aria-pressed={picked}
          aria-label={`Light the path to ${c.file} ${c.line} on the map`}
          onClick={onPick}
          className="-m-1 flex cursor-pointer flex-col gap-1.5 rounded p-1 text-left transition-colors hover:bg-ds-gray-subtlest focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ds-brand-subtler"
        >
          {body}
        </button>
      ) : (
        <div className="flex flex-col gap-1.5">{body}</div>
      )}
      <div className="flex items-center gap-2 border-t border-ds-border-subtlest pt-1.5">
        <FixNowDialog c={c} />
      </div>
    </div>
  );
}
