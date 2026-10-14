import { useCallback, useMemo, useState } from 'react';
import {
  parseAsBoolean,
  parseAsString,
  useQueryStates,
} from 'nuqs';
import {
  parseCategories,
  pickHistory,
  serializeCategories,
  type Category,
} from '@/core/sessionLayout';

/** `ev`: the chip selection; `ctx`: the Context lane. */
const CATEGORY_PARAMS = {
  ev: parseAsString,
  ctx: parseAsBoolean,
};

/**
 * The dock's category selection and the Context lane toggle, in the URL so
 * reload / share keep them. Not part of the replay scope: changing them
 * never remounts the replay (playback keeps its place).
 */
export function useEventCategories() {
  const [q, setQ] = useQueryStates(CATEGORY_PARAMS);
  const selected = useMemo(() => parseCategories(q.ev), [q.ev]);
  const context = q.ctx ?? false;
  // Click order this visit (see pickHistory): what "the last chip" means.
  const [picks, setPicks] = useState<Category[]>([]);
  const setSelected = useCallback(
    (next: ReadonlySet<Category>) => {
      setPicks((h) => pickHistory(h, selected, next));
      void setQ({
        ev: serializeCategories(next),
        ctx: context || null,
      });
    },
    [setQ, context, selected]
  );
  const setContext = useCallback(
    (on: boolean) =>
      void setQ({
        ctx: on || null,
        ev: serializeCategories(selected),
      }),
    [setQ, selected]
  );
  return { selected, setSelected, picks, context, setContext };
}

export type EventCategories = ReturnType<typeof useEventCategories>;
