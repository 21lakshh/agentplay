/**
 * Chart palette — the single source of colour for every chart in the app.
 *
 * Four jobs, four rules:
 *  - identity  (which series)  -> SERIES, in fixed slot order, never cycled
 *  - magnitude (how much)      -> SEQUENTIAL, one hue, light to dark
 *  - polarity  (which side)    -> DIVERGING, two hues + neutral midpoint
 *  - state     (good..bad)     -> STATUS, reserved meaning, always icon + label
 *
 * Every value is a CSS variable, so light and dark flip without a `dark:`
 * prefix and without JS theme state.
 */

/** Categorical identity slots. Fixed order — assign in sequence, never cycle. */
export const SERIES = [
  'var(--ds-series-1)',
  'var(--ds-series-2)',
  'var(--ds-series-3)',
  'var(--ds-series-4)',
  'var(--ds-series-5)',
  'var(--ds-series-6)',
  'var(--ds-series-7)',
  'var(--ds-series-8)',
] as const;

/** Everything past slot 8, and the de-emphasised rest in an emphasis chart. */
export const SERIES_OTHER = 'var(--ds-series-other)';

/**
 * Colour for series `index`. Past slot 8 you get the neutral "other" grey,
 * because a generated 9th hue is indistinguishable under colour blindness.
 * Fold the tail into an explicit "Other" bucket instead of leaning on this.
 */
export function seriesColor(index: number): string {
  return SERIES[index] ?? SERIES_OTHER;
}

/** Single series, and the "one hue + grey" emphasis chart. */
export const PRIMARY = 'var(--ds-brand-subtler)';
/** Active dot ring and other accents that sit on top of PRIMARY. */
export const PRIMARY_ACCENT = 'var(--ds-brand-subtle)';

/** Magnitude ramp, low to high. One hue, monotone lightness. */
export const SEQUENTIAL = [
  'var(--primitive-brand-green-200)',
  'var(--primitive-brand-green-300)',
  'var(--primitive-brand-green-400)',
  'var(--primitive-brand-green-500)',
  'var(--primitive-brand-green-600)',
] as const;

/** Intensity ramp with no hue at all (activity heatmap). */
export const INTENSITY = [
  'var(--ds-heatmap-1)',
  'var(--ds-heatmap-2)',
  'var(--ds-heatmap-3)',
  'var(--ds-heatmap-4)',
  'var(--ds-heatmap-5)',
] as const;

/** Polarity: two poles and a neutral midpoint. Never a hue in the middle. */
export const DIVERGING = {
  negative: 'var(--ds-danger-default)',
  neutral: 'var(--primitive-gray-300)',
  positive: 'var(--ds-success-default)',
} as const;

/**
 * Reserved state colours. Never reuse one of these as "series 4" — a status
 * colour must only ever mean its state, and always ships with an icon or label.
 */
export const STATUS = {
  good: 'var(--ds-success-default)',
  info: 'var(--ds-blue-default)',
  warning: 'var(--ds-warning-default)',
  critical: 'var(--ds-danger-default)',
} as const;

/** Chart chrome. */
export const CHROME = {
  grid: 'var(--ds-chart-grid)',
  axis: 'var(--ds-chart-axis)',
  cursor: 'var(--ds-chart-cursor)',
  /** Donut / meter track ring. */
  track: 'var(--primitive-gray-100)',
} as const;
