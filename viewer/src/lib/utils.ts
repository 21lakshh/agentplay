import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

const NAMESPACES = [
  'gray',
  'border',
  'warning',
  'brand',
  'text',
  'success',
  'danger',
] as const;
const SCALE = [
  'lightest',
  'subtlest',
  'subtler',
  'subtle',
  'default',
  'darker',
  'darkest',
];
const SURFACES = ['white', 'off-white', 'dark'];

const dsClasses = (prefix: 'text' | 'bg' | 'border' | 'divide') => [
  ...NAMESPACES.flatMap((ns) => SCALE.map((s) => `${prefix}-ds-${ns}-${s}`)),
  ...SURFACES.map((s) => `${prefix}-ds-surface-${s}`),
];

const customTwMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      'text-color': dsClasses('text'),
      'bg-color': dsClasses('bg'),
      'border-color': dsClasses('border'),
    },
  },
});

export function cn(...inputs: ClassValue[]) {
  return customTwMerge(clsx(inputs));
}
