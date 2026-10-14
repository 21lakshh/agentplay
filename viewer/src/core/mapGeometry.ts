/**
 * Radial map geometry: a plain circle (leaves on the ring R), a viewBox
 * whose vertical padding is only as tall as the labels need, and per-label
 * truncation so a label near the top or bottom never runs past it. Pure;
 * tested in `mapGeometry.test.ts`.
 *
 * Why the vertical padding matters: the map's pane is wider than tall, so
 * the SVG scales to fit its height (`xMidYMid meet`). Horizontal room is
 * cheap (PAD_X is sized so a full-length side label fits), but anything above or
 * below the viewBox is clipped -- and every point of vertical padding is
 * circle radius lost. Labels are rotated along their spokes, so a label at
 * angle `a` rises `len * |sin a|`; only labels near the top and bottom need
 * the padding, and those are shortened to fit it.
 */

export const MAP_RADIUS = 250;
/** Horizontal room for the leaf labels on the left / right: enough for a
 * full LABEL_MAX_CHARS label at the largest leaf font (14px) starting just
 * outside the ring, so side labels are never cut by the SVG's own box. The
 * pane is landscape and the map height-bound, so this extra width does not
 * shrink the circle. That holds while the pane is at least ~1.55x as wide as
 * tall (the typical desktop layout); a narrower pane scales to its width and
 * gives up some radius -- a deliberate trade for side labels that are never
 * clipped. */
export const PAD_X = 220;
/** Vertical padding bounds: the floor covers the pulse ring and ×N badges
 * around a leaf at the very top; the cap is what the circle gives up at
 * most (labels longer than that near the top are shortened). */
export const PAD_Y_MIN = 24;
export const PAD_Y_MAX = 80;
/** Longest label, in characters (the rest is in the tooltip). */
export const LABEL_MAX_CHARS = 24;
/** Monospace advance, in em (a conservative upper bound). */
const CHAR_EM = 0.62;
/** Clearance kept between a label's end and the viewBox edge (text halo). */
const EDGE = 3;

/** Where one rotated label sits: it starts at radius `r0` on angle `a`, in a
 * `font`-px monospace, and is `len` characters long untruncated. */
export interface LabelSpot {
  r0: number;
  a: number;
  font: number;
  len: number;
}

/** How far (|y|) a label of `chars` characters reaches from the centre. */
export function labelExtentY(s: LabelSpot, chars: number): number {
  const along = s.r0 + chars * s.font * CHAR_EM;
  return (
    along * Math.abs(Math.sin(s.a)) +
    (s.font / 2) * Math.abs(Math.cos(s.a)) +
    EDGE
  );
}

/** The vertical padding the labels need, clamped to [PAD_Y_MIN, PAD_Y_MAX]. */
export function mapPadY(spots: readonly LabelSpot[], R = MAP_RADIUS): number {
  let need = PAD_Y_MIN;
  for (const s of spots)
    need = Math.max(
      need,
      labelExtentY(s, Math.min(s.len, LABEL_MAX_CHARS)) - R
    );
  return Math.ceil(Math.min(PAD_Y_MAX, need));
}

/** The most characters a label may show and still end inside the viewBox,
 * both vertically (R + padY) and horizontally (R + PAD_X) -- the SVG clips
 * at its own box, so a side label is not free to run past the viewBox
 * either (at least 3, at most LABEL_MAX_CHARS). */
export function labelChars(s: LabelSpot, padY: number, R = MAP_RADIUS): number {
  const sin = Math.abs(Math.sin(s.a));
  const cos = Math.abs(Math.cos(s.a));
  let room = Infinity;
  if (sin > 1e-6)
    room = Math.min(room, (R + padY - EDGE - (s.font / 2) * cos) / sin - s.r0);
  if (cos > 1e-6)
    room = Math.min(room, (R + PAD_X - EDGE - (s.font / 2) * sin) / cos - s.r0);
  if (!Number.isFinite(room)) return LABEL_MAX_CHARS;
  const n = Math.floor(room / (s.font * CHAR_EM));
  return Math.max(3, Math.min(LABEL_MAX_CHARS, n));
}

/** The SVG viewBox of a circle of radius R with the given vertical padding. */
export const mapViewBox = (padY: number, R = MAP_RADIUS): string =>
  `${-R - PAD_X} ${-R - padY} ${2 * (R + PAD_X)} ${2 * (R + padY)}`;

/** `s` cut to at most `n` characters, the last one an ellipsis. */
export const truncateLabel = (s: string, n: number): string =>
  s.length > n ? `${s.slice(0, Math.max(1, n - 1))}…` : s;
