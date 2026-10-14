/**
 * The one file in the viewer that knows an Entelligence URL.
 *
 * The README promises the viewer "loads no remote fonts or scripts" and that
 * the package makes no network calls except the optional email signup and
 * disconnect. Both stay true here: nothing in this file fetches anything. It
 * builds a string for an `<a href>` the user has to click, and `grep -rn "entelligence.ai" src/` returns this file and its test,
 * so the claim stays checkable in under a minute.
 *
 * Nothing from the session goes into a link. The source tag is a constant per
 * call site -- no repo name, no path, no identifier.
 */

import type { SessionMapPr } from './schema';

export const ENTELLIGENCE_HOME = 'https://entelligence.ai/';

/** The research write-up the welcome card links to: why agentplay exists. */
export const SELF_IMPROVING_AGENTS = `${ENTELLIGENCE_HOME}research/self-improving-coding-agents`;

/** The surface a locked block was clicked from. A fixed vocabulary, so a
 * reviewer can see the whole set of things a link can say about the user. */
export type LinkSource =
  | 'outcomes'
  | 'pr-menu'
  | 'review-tab'
  | 'fix-dialog'
  | 'welcome'
  | 'banner';

/** Where a locked block's call to action points: the public site's home
 * page. There is no waitlist to send anyone to yet, so every call to action
 * lands in the same place and says the same thing. */
export function connectUrl(src: LinkSource): string {
  return `${ENTELLIGENCE_HOME}?src=agentplay-viewer&from=${src}`;
}

/** The write-up, tagged with the surface it was opened from. */
export function researchUrl(src: LinkSource): string {
  return `${SELF_IMPROVING_AGENTS}?src=agentplay-viewer&from=${src}`;
}

export interface ReviewLoopCopy {
  title: string;
  blurb: string;
  /** The PR the blurb names, for the row under the call to action. */
  pr: SessionMapPr | null;
}

/** "#133" on its own, or "owner/repo #133" when the map recorded the repo. */
export const prLabel = (pr: SessionMapPr): string =>
  pr.repo ? `${pr.repo} #${pr.number}` : `#${pr.number}`;

/**
 * What the outcomes block says. With a PR in the map it names that PR and the
 * step it was opened at, because the map already carries both -- few sessions
 * open one, and the specific sentence is the whole reason to show this rather
 * than a standing banner.
 *
 * The first PR wins. A session that opens several is rarer still, and naming
 * one is better than a list nobody reads.
 */
export function reviewLoopCopy(prs: readonly SessionMapPr[]): ReviewLoopCopy {
  const pr = prs[0] ?? null;
  if (!pr) {
    return {
      title: 'Review comments, traced to the edit behind them',
      blurb:
        'When a session opens a pull request, Entelligence links each review comment back to the edit that caused it, and clusters the repeats into a rule your agent gets next time.',
      pr: null,
    };
  }
  return {
    title: 'Review comments on this PR',
    blurb: `This session opened ${prLabel(pr)} at step ${pr.seq}. Entelligence traces each review comment on it back to the edit that caused it.`,
    pr,
  };
}
