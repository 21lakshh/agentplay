import { createContext, useContext, type ReactNode } from 'react';
import type { SessionMapSource } from './source';

const SourceContext = createContext<SessionMapSource | null>(null);

/** Inject the data source every Session Explorer hook reads from. */
export function SessionMapSourceProvider({
  source,
  children,
}: {
  source: SessionMapSource;
  children: ReactNode;
}) {
  return (
    <SourceContext.Provider value={source}>{children}</SourceContext.Provider>
  );
}

export function useSessionMapSource(): SessionMapSource {
  const source = useContext(SourceContext);
  if (!source) {
    throw new Error('useSessionMapSource needs a SessionMapSourceProvider');
  }
  return source;
}
