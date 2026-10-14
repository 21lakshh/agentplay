import { createContext, useContext, type ReactNode } from 'react';

/**
 * The bundled demo session that opened a PR (`scripts/make-demo.mjs`, first
 * entry). It is the only demo session with a PR in it, so it is the one worth
 * swapping to when someone asks what the review loop looks like.
 *
 * A wrong id here is not fatal: the explorer falls back to the first session
 * in the list, which is this one.
 */
export const SAMPLE_SESSION_ID =
  'local:claude-code:5f2c9a1e-7b44-4d0e-9c1a-demo00000001';

/**
 * The URL params that decide which sessions the explorer's list can offer:
 * the sessions search box and the time window. Anything else that filters
 * that list belongs here too.
 */
export const LIST_FILTER_PARAMS = ['sq', 'win'] as const;

/** The query "See sample" pushes. Null clears a param. */
export interface SampleQuery {
  demo: string | null;
  sample: string | null;
  session: string | null;
  view: string | null;
  /** the sessions search (`LIST_FILTER_PARAMS`) */
  sq: string | null;
  /** the time window (`LIST_FILTER_PARAMS`) */
  win: string | null;
}

/**
 * Showing the sample swaps the view onto `SAMPLE_SESSION_ID` *and* clears
 * every list filter, because the explorer falls back to the first row of the
 * **filtered** list when the session it is asked for is not in it: a search
 * or a time window that excludes the sample would otherwise leave the reader
 * on some other session with the preview still blurred.
 *
 * It is a pushed history entry, so the browser's own Back button restores the
 * filters the reader had -- which is why they are cleared through the URL
 * rather than as local state.
 */
export const SAMPLE_QUERY: SampleQuery = {
  demo: '1',
  sample: '1',
  session: SAMPLE_SESSION_ID,
  view: null,
  sq: null,
  win: null,
};

/**
 * Whether the viewer renders the files the reader opened rather than its base
 * source. The sample outranks them while it is on, but only bypasses them:
 * "See sample" leaves the opened-file selection alone so that leaving the
 * sample -- including by a Back that restores the URL and nothing else --
 * returns to the file-backed session instead of losing it.
 */
export const showsOpenedFiles = (s: {
  showFiles: boolean;
  fileCount: number;
  sampleActive: boolean;
}): boolean => s.showFiles && s.fileCount > 0 && !s.sampleActive;

export interface SampleMode {
  /** the viewer is showing the bundled sample instead of the user's own */
  active: boolean;
  /** a local server answered, so there is something to go back to */
  canExit: boolean;
  /** swap the list, the map and the panels onto the sample session */
  show: () => void;
  /** back to the user's own sessions */
  exit: () => void;
}

const NO_SAMPLE: SampleMode = {
  active: false,
  canExit: false,
  show: () => {},
  exit: () => {},
};

const SampleModeContext = createContext<SampleMode>(NO_SAMPLE);

/** Lets a panel deep in the explorer swap the whole view onto the sample
 * session without threading four callbacks through every layer between. */
export function SampleModeProvider({
  value,
  children,
}: {
  value: SampleMode;
  children: ReactNode;
}) {
  return (
    <SampleModeContext.Provider value={value}>
      {children}
    </SampleModeContext.Provider>
  );
}

export const useSampleMode = (): SampleMode => useContext(SampleModeContext);
