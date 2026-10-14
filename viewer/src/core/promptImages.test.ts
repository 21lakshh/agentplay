import { describe, expect, it } from 'vitest';
import {
  imageLabel,
  mediaTypeLabel,
  pasteTag,
  promptImageTurns,
} from './promptImages';
import { SESSION_MAP_SCHEMA_V1, sessionMapSchema } from './schema';

type RawEvent = Record<string, unknown> & { kind: string };

function makeMap(events: RawEvent[]) {
  return sessionMapSchema.parse({
    schema: SESSION_MAP_SCHEMA_V1,
    session: {
      id: 'local:claude-code:s1',
      startedAt: '2026-09-23T15:44:00Z',
    },
    totals: { calls: 0, requests: 0 },
    agents: [
      { id: 'main', parent: null, kind: 'main' },
      { id: 'sub1', parent: 'main', kind: 'subagent' },
    ],
    events: events.map((e, i) => ({ seq: i, t: i * 1000, agent: 'main', ...e })),
  });
}

describe('promptImageTurns', () => {
  it('numbers turns over main-agent user events only, across mixed events', () => {
    const map = makeMap([
      { kind: 'user', task: 1, images: 1 }, // turn 1
      { kind: 'llm', task: 1 },
      { kind: 'user', task: 1, agent: 'sub1', images: 5 }, // subagent: no turn
      { kind: 'exec', task: 1 },
      { kind: 'user', task: 2 }, // turn 2, no images
      { kind: 'llm', task: 2 },
      { kind: 'user', task: 2, images: 2 }, // turn 3
      { kind: 'user', task: 3, images: 4 }, // turn 4
    ]);
    expect(promptImageTurns(map, 1)).toEqual([{ turn: 1, count: 1 }]);
    expect(promptImageTurns(map, 2)).toEqual([{ turn: 3, count: 2 }]);
    expect(promptImageTurns(map, 3)).toEqual([{ turn: 4, count: 4 }]);
  });

  it('keeps global numbering and excludes events outside the task window', () => {
    const map = makeMap([
      { kind: 'user', task: 1, images: 3 },
      { kind: 'user', task: 2, images: 1 },
      { kind: 'user', task: 2, images: 2 },
    ]);
    expect(promptImageTurns(map, 2)).toEqual([
      { turn: 2, count: 1 },
      { turn: 3, count: 2 },
    ]);
    expect(promptImageTurns(map, 9)).toEqual([]);
  });

  it('returns nothing when no turn carried images', () => {
    const map = makeMap([
      { kind: 'user', task: 1 },
      { kind: 'user', task: 1, images: 0 },
      { kind: 'llm', task: 1 },
    ]);
    expect(promptImageTurns(map, 1)).toEqual([]);
  });
});

describe('labels', () => {
  it('uses the paste id, falling back to the 1-based position', () => {
    expect(pasteTag(2, 0)).toBe('#2');
    expect(pasteTag(null, 0)).toBe('#1');
    expect(imageLabel(null, 2, 'image/png')).toBe('#3 · PNG');
  });

  it('labels media types', () => {
    expect(mediaTypeLabel('image/png')).toBe('PNG');
    expect(mediaTypeLabel('image/jpeg')).toBe('JPEG');
    expect(mediaTypeLabel('image/svg+xml')).toBe('SVG');
    expect(mediaTypeLabel('')).toBe('');
    expect(imageLabel(2, 0, '')).toBe('#2');
    expect(imageLabel(2, 0, 'image/webp')).toBe('#2 · WEBP');
  });
});
