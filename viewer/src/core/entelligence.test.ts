import { describe, expect, it } from 'vitest';
import {
  researchUrl,
  ENTELLIGENCE_HOME,
  prLabel,
  reviewLoopCopy,
  connectUrl,
} from './entelligence';
import type { SessionMapPr } from './schema';

const pr = (over: Partial<SessionMapPr> = {}): SessionMapPr =>
  ({
    number: 133,
    url: 'https://github.com/owner/repo/pull/133',
    seq: 1046,
    state: 'open',
    repo: 'owner/repo',
    provider: 'github',
    ...over,
  }) as SessionMapPr;

describe('connectUrl', () => {
  it('points at the public site and nowhere else', () => {
    expect(connectUrl('outcomes')).toBe(
      'https://entelligence.ai/?src=agentplay-viewer&from=outcomes'
    );
    expect(connectUrl('pr-menu').startsWith(ENTELLIGENCE_HOME)).toBe(true);
  });

  it('carries nothing but the source tag', () => {
    const q = new URL(connectUrl('outcomes')).searchParams;
    expect([...q.keys()].sort()).toEqual(['from', 'src']);
  });
});

describe('prLabel', () => {
  it('uses the repo when the map recorded one', () => {
    expect(prLabel(pr())).toBe('owner/repo #133');
  });

  it('falls back to the bare number', () => {
    expect(prLabel(pr({ repo: null }))).toBe('#133');
  });
});

describe('reviewLoopCopy', () => {
  it('names the PR and the step it was opened at', () => {
    const copy = reviewLoopCopy([pr()]);
    expect(copy.blurb).toContain('owner/repo #133');
    expect(copy.blurb).toContain('step 1046');
    expect(copy.pr?.number).toBe(133);
  });

  it('takes the first PR when a session opened several', () => {
    expect(reviewLoopCopy([pr(), pr({ number: 134 })]).pr?.number).toBe(133);
  });

  it('falls back to the generic line with no PR', () => {
    const copy = reviewLoopCopy([]);
    expect(copy.pr).toBeNull();
    expect(copy.blurb).not.toContain('step');
  });

  it('never puts session data in the link', () => {
    expect(connectUrl('outcomes')).not.toContain('owner/repo');
  });
});

describe('researchUrl', () => {
  it('points at the write-up and says only which surface opened it', () => {
    expect(researchUrl('welcome')).toBe(
      'https://entelligence.ai/research/self-improving-coding-agents?src=agentplay-viewer&from=welcome'
    );
  });
});
