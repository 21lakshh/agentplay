import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  LIST_FILTER_PARAMS,
  SAMPLE_QUERY,
  SAMPLE_SESSION_ID,
  showsOpenedFiles,
} from './sampleMode';

/**
 * "See sample" swaps the whole viewer onto the bundled session the review
 * comments are about. Two things went wrong with that, and both of them are
 * about state the swap did not own: the filters that decide whether the
 * sample is reachable at all, and the file the reader had opened.
 */
describe('the query "See sample" pushes', () => {
  it('asks for the sample session, in the bundled sessions', () => {
    expect(SAMPLE_QUERY.session).toBe(SAMPLE_SESSION_ID);
    expect(SAMPLE_QUERY.demo).toBe('1');
    // ...with the locked block revealed, which is the point of going there.
    expect(SAMPLE_QUERY.sample).toBe('1');
  });

  it('names a session the bundled sessions have', () => {
    const index = JSON.parse(
      fs.readFileSync(path.resolve(__dirname, '../../public/demo/index.json'), 'utf8')
    ) as { sessions: { sessionId: string }[] };
    expect(index.sessions.map((s) => s.sessionId)).toContain(SAMPLE_SESSION_ID);
  });

  it('clears every filter the sessions list applies', () => {
    for (const key of LIST_FILTER_PARAMS)
      expect(SAMPLE_QUERY[key], `${key} is cleared`).toBeNull();
    expect([...LIST_FILTER_PARAMS]).toEqual(['sq', 'win']);
  });

  it('does not clear them by leaving them out of the query', () => {
    // nuqs leaves an omitted key alone, so an absent key is not a cleared
    // one: each has to be present and null.
    for (const key of LIST_FILTER_PARAMS)
      expect(Object.hasOwn(SAMPLE_QUERY, key), `${key} is written`).toBe(true);
  });
});

describe('the sample over an opened file', () => {
  const opened = { showFiles: true, fileCount: 1 };

  it('bypasses the opened file rather than closing it', () => {
    expect(showsOpenedFiles({ ...opened, sampleActive: true })).toBe(false);
    // Leaving the sample is only the URL changing -- the browser's Back
    // button restores nothing else -- so the opened file has to come back
    // from state the swap never touched.
    expect(showsOpenedFiles({ ...opened, sampleActive: false })).toBe(true);
  });

  it('shows the base source when no file is open', () => {
    expect(
      showsOpenedFiles({ showFiles: false, fileCount: 1, sampleActive: false })
    ).toBe(false);
    // Nothing opened yet: `showFiles` can be true before a file lands.
    expect(
      showsOpenedFiles({ showFiles: true, fileCount: 0, sampleActive: false })
    ).toBe(false);
  });
});
