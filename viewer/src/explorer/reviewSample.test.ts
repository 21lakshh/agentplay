import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { SAMPLE_COMMENTS } from './reviewSample';

/**
 * The sample comments cite step numbers and files from the bundled demo
 * session, and "See sample" puts the reader on that very session. If the
 * demo is regenerated and a step moves, a citation here becomes a lie that
 * anyone can check by scrubbing the timeline. These tests are what stop
 * that happening quietly.
 */
interface DemoMap {
  files: { id: number; path: string }[];
  tasks: { name: string; startSeq: number; endSeq: number }[];
  events: {
    seq: number;
    kind: string;
    tool?: string;
    targets?: { file?: number; touch?: string }[];
  }[];
}

const demo = JSON.parse(
  readFileSync('public/demo/claude-code-rate-limit.json', 'utf8')
) as DemoMap;

const bySeq = new Map(demo.events.map((e) => [e.seq, e]));
const pathOf = (id: number | undefined) =>
  demo.files.find((f) => f.id === id)?.path;
const taskAt = (seq: number) =>
  demo.tasks.find((t) => seq >= t.startSeq && seq <= t.endSeq)?.name;

describe('the sample comments are anchored in the demo session', () => {
  it.each(SAMPLE_COMMENTS)('$file $line', (c) => {
    const edit = bySeq.get(c.editSeq);
    expect(edit, `step #${c.editSeq} is in the demo map`).toBeDefined();
    expect(edit?.kind, `step #${c.editSeq} is an edit`).toBe('edit');
    // The comment is on the file that edit actually touched.
    expect(edit?.targets?.map((t) => pathOf(t.file))).toContain(c.file);
    // And it is filed under the task that edit falls in.
    expect(taskAt(c.editSeq)).toBe(c.task);
  });

  it('cites only steps the demo session has, in order', () => {
    for (const c of SAMPLE_COMMENTS) {
      const seqs = c.path.map((s) => s.seq);
      expect(seqs).toEqual([...seqs].sort((a, b) => a - b));
      for (const s of seqs) expect(bySeq.has(s)).toBe(true);
      // The path ends at the edit the comment is attributed to.
      expect(seqs).toContain(c.editSeq);
    }
  });

  it('calls a ghost only on a file the session never touched', () => {
    const touched = new Set(demo.files.map((f) => f.path));
    for (const c of SAMPLE_COMMENTS) {
      if (!c.ghost) continue;
      const named = c.ghost.split(' ')[0];
      expect(touched.has(named), `${named} is absent from the map`).toBe(false);
    }
  });

  it('shows more than one issue', () => {
    expect(SAMPLE_COMMENTS.length).toBeGreaterThan(1);
  });
});
