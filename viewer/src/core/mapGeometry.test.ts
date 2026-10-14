import { describe, expect, it } from 'vitest';
import {
  LABEL_MAX_CHARS,
  MAP_RADIUS,
  PAD_X,
  PAD_Y_MAX,
  PAD_Y_MIN,
  labelChars,
  labelExtentY,
  mapPadY,
  mapViewBox,
  truncateLabel,
  type LabelSpot,
} from './mapGeometry';

const R = MAP_RADIUS;
/** A leaf label spot at angle `a` (radians from +x, y down). */
const leaf = (a: number, len: number): LabelSpot => ({
  r0: R + 8,
  a,
  font: 13.5,
  len,
});
const TOP = -Math.PI / 2;

describe('map geometry', () => {
  it('is a plain circle: the viewBox is R + padding on each side', () => {
    expect(mapViewBox(60)).toBe(
      `${-R - PAD_X} ${-R - 60} ${2 * (R + PAD_X)} ${2 * (R + 60)}`
    );
  });

  it('never lets a label run past the viewBox horizontally, at any angle', () => {
    // Largest leaf font; the SVG clips at its own box, so side labels need
    // the same guarantee as the top / bottom ones.
    for (let k = 0; k < 72; k++) {
      const a = (k / 72) * 2 * Math.PI;
      const s: LabelSpot = { r0: R + 8, a, font: 14, len: 60 };
      const n = labelChars(s, PAD_Y_MAX);
      const xEnd =
        (s.r0 + n * s.font * 0.62) * Math.abs(Math.cos(a)) +
        (s.font / 2) * Math.abs(Math.sin(a));
      expect(xEnd).toBeLessThanOrEqual(R + PAD_X + 1e-9);
    }
    // ...and PAD_X is wide enough that a straight side label keeps its full length.
    expect(labelChars({ r0: R + 8, a: 0, font: 14, len: 60 }, PAD_Y_MAX)).toBe(
      LABEL_MAX_CHARS
    );
  });

  it('pads only as much as the top / bottom labels need, within bounds', () => {
    // side labels need no vertical room at all
    expect(mapPadY([leaf(0, 24), leaf(Math.PI, 24)])).toBe(PAD_Y_MIN);
    // a short label straight up needs a little
    const short = mapPadY([leaf(TOP, 4)]);
    expect(short).toBeGreaterThan(PAD_Y_MIN);
    expect(short).toBeLessThan(PAD_Y_MAX);
    // a long one is capped (it gets shortened instead)
    expect(mapPadY([leaf(TOP, 24)])).toBe(PAD_Y_MAX);
    expect(mapPadY([])).toBe(PAD_Y_MIN);
  });

  it('never lets a shown label run past the viewBox edge', () => {
    for (const len of [3, 12, 24, 60]) {
      const spots = Array.from({ length: 72 }, (_, k) =>
        leaf((k / 72) * 2 * Math.PI, len)
      );
      const padY = mapPadY(spots);
      for (const s of spots) {
        const n = Math.min(s.len, labelChars(s, padY));
        expect(labelExtentY(s, n)).toBeLessThanOrEqual(R + padY + 1e-9);
      }
    }
  });

  it('keeps full labels on the sides and shortens only near the top', () => {
    expect(labelChars(leaf(0, 40), PAD_Y_MAX)).toBe(LABEL_MAX_CHARS);
    expect(labelChars(leaf(Math.PI / 5, 40), PAD_Y_MAX)).toBe(LABEL_MAX_CHARS);
    const top = labelChars(leaf(TOP, 40), PAD_Y_MAX);
    expect(top).toBeGreaterThanOrEqual(3);
    expect(top).toBeLessThan(LABEL_MAX_CHARS);
  });

  it('truncates with an ellipsis within the budget', () => {
    expect(truncateLabel('proxy.ts', 24)).toBe('proxy.ts');
    expect(truncateLabel('SessionMapSvg.tsx', 8)).toBe('Session…');
    expect(truncateLabel('SessionMapSvg.tsx', 8)).toHaveLength(8);
  });
});
