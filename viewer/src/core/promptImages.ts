import type { SessionMap } from './schema';

/** One user turn that carried pasted images. */
export interface PromptImageTurn {
  /** 1-based, counted over the whole session's main-agent user events */
  turn: number;
  /** images the map recorded on that turn */
  count: number;
}

/**
 * The turns of one task that carried images, numbered the way the server
 * numbers them: the builder counts every `user` event of the `main` agent in
 * array order, so the k-th such event is turn k for the whole session, not
 * for the task. `map` must therefore be the full map (never a scoped or lens
 * view, which drops events and shifts the count). A task's window is the
 * events stamped with its number, the same windowing `taskSummaries` uses.
 */
export function promptImageTurns(
  map: Pick<SessionMap, 'events'>,
  task: number
): PromptImageTurn[] {
  const out: PromptImageTurn[] = [];
  let turn = 0;
  for (const e of map.events) {
    if (e.kind !== 'user' || e.agent !== 'main') continue;
    turn += 1;
    const count = e.images ?? 0;
    if (e.task === task && count > 0) out.push({ turn, count });
  }
  return out;
}

/** "#2" for a paste id, else the image's 1-based position in its task. */
export function pasteTag(pasteId: number | null, i: number): string {
  return `#${pasteId ?? i + 1}`;
}

/** "PNG" from "image/png"; "SVG" from "image/svg+xml"; '' when unknown. */
export function mediaTypeLabel(mediaType: string): string {
  const sub = mediaType.split('/')[1]?.split('+')[0]?.trim();
  return sub ? sub.toUpperCase() : '';
}

/** Thumbnail overlay text: "#2 · PNG". */
export function imageLabel(
  pasteId: number | null,
  i: number,
  mediaType: string
): string {
  const type = mediaTypeLabel(mediaType);
  const tag = pasteTag(pasteId, i);
  return type ? `${tag} · ${type}` : tag;
}
