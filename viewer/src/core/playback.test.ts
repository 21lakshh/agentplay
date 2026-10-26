import { describe, expect, it } from 'vitest';
import {
  MS_PER_EVENT,
  SPEEDS,
  initialPlayback,
  playbackReducer,
  replayKeyAction,
  type PlaybackAction,
  type PlaybackState,
} from './playback';

const run = (s: PlaybackState, ...actions: PlaybackAction[]) =>
  actions.reduce(playbackReducer, s);

const loaded = (n = 100, start = 10) =>
  playbackReducer(initialPlayback, { type: 'load', n, start });

describe('playbackReducer', () => {
  it('loads paused and clamps the start', () => {
    expect(loaded(5, 99).p).toBe(4);
    expect(loaded(5, -3).p).toBe(0);
    expect(loaded(0, 3).p).toBe(0);
    expect(loaded().playing).toBe(false);
  });

  it('seek and step clamp and pause', () => {
    const playing = run(loaded(), { type: 'play' });
    expect(playing.playing).toBe(true);
    const sought = playbackReducer(playing, { type: 'seek', index: 500 });
    expect(sought.p).toBe(99);
    expect(sought.playing).toBe(false);
    expect(run(loaded(), { type: 'step', delta: -50 }).p).toBe(0);
    expect(run(loaded(), { type: 'step', delta: 10 }).p).toBe(20);
  });

  it('play at the end restarts from the top', () => {
    const s = run(loaded(), { type: 'seek', index: 99 }, { type: 'play' });
    expect(s.p).toBe(0);
    expect(s.playing).toBe(true);
  });

  it('does not play a single-event session', () => {
    expect(run(loaded(1, 0), { type: 'play' }).playing).toBe(false);
  });

  it('toggle flips play and pause', () => {
    const s = run(loaded(), { type: 'toggle' });
    expect(s.playing).toBe(true);
    expect(playbackReducer(s, { type: 'toggle' }).playing).toBe(false);
  });

  it('cycles speeds 1x, 4x, 16x', () => {
    const s0 = loaded();
    expect(SPEEDS[s0.speedIdx]).toBe(4);
    const s1 = playbackReducer(s0, { type: 'cycleSpeed' });
    expect(SPEEDS[s1.speedIdx]).toBe(16);
    const s2 = playbackReducer(s1, { type: 'cycleSpeed' });
    expect(SPEEDS[s2.speedIdx]).toBe(1);
  });

  it('ticks advance by elapsed time times speed', () => {
    let s = run(loaded(), { type: 'play' }); // 4x
    s = playbackReducer(s, { type: 'tick', dtMs: MS_PER_EVENT / 8 });
    expect(s.p).toBe(10); // 0.5 event accumulated
    s = playbackReducer(s, { type: 'tick', dtMs: MS_PER_EVENT / 8 });
    expect(s.p).toBe(11);
    s = playbackReducer(s, { type: 'tick', dtMs: MS_PER_EVENT });
    expect(s.p).toBe(15);
  });

  it('stops at the last event', () => {
    let s = run(loaded(20, 18), { type: 'play' });
    s = playbackReducer(s, { type: 'tick', dtMs: MS_PER_EVENT * 10 });
    expect(s.p).toBe(19);
    expect(s.playing).toBe(false);
    expect(playbackReducer(s, { type: 'tick', dtMs: 1000 })).toBe(s);
  });

  it('ignores ticks while paused', () => {
    const s = loaded();
    expect(playbackReducer(s, { type: 'tick', dtMs: 1000 })).toBe(s);
  });
});

describe('replayKeyAction', () => {
  const key = (k: string, mods: Partial<Record<string, boolean>> = {}) =>
    replayKeyAction({
      key: k,
      shiftKey: false,
      metaKey: false,
      ctrlKey: false,
      altKey: false,
      ...mods,
    });

  it('space toggles, arrows step (Shift steps 10)', () => {
    expect(key(' ')).toEqual({ type: 'toggle' });
    expect(key('ArrowRight')).toEqual({ type: 'step', delta: 1 });
    expect(key('ArrowLeft')).toEqual({ type: 'step', delta: -1 });
    expect(key('ArrowLeft', { shiftKey: true })).toEqual({
      type: 'step',
      delta: -10,
    });
  });

  it('Home and End seek to start and end', () => {
    expect(key('Home')).toEqual({ type: 'seek', index: 0 });
    expect(key('End')).toEqual({ type: 'seek', index: Infinity });
  });

  it('E / X / D / O / R jump, either case', () => {
    expect(key('e')).toEqual({ type: 'jump', kind: 'edit' });
    expect(key('X')).toEqual({ type: 'jump', kind: 'err' });
    expect(key('d')).toEqual({ type: 'jump', kind: 'delay' });
    expect(key('O')).toEqual({ type: 'jump', kind: 'out' });
    expect(key('r')).toEqual({ type: 'jump', kind: 'retry' });
  });

  it('other keys and browser shortcuts map to nothing', () => {
    expect(key('q')).toBeNull();
    expect(key('Enter')).toBeNull();
    expect(key('constructor')).toBeNull();
    expect(key('e', { metaKey: true })).toBeNull();
    expect(key(' ', { ctrlKey: true })).toBeNull();
    expect(key('ArrowRight', { altKey: true })).toBeNull();
    expect(key('Home', { ctrlKey: true })).toBeNull();
    expect(key('Home', { metaKey: true })).toBeNull();
    expect(key('Home', { altKey: true })).toBeNull();
    expect(key('End', { ctrlKey: true })).toBeNull();
    expect(key('End', { metaKey: true })).toBeNull();
    expect(key('End', { altKey: true })).toBeNull();
  });
});