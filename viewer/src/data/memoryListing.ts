import type {
  SessionExplorerSession,
  SessionExplorerSessionsPage,
} from '@/core/schema';
import type { SessionsParams } from './source';

/**
 * Server-equivalent listing over rows held in memory: date window and search
 * filter, newest first, and an opaque offset cursor, so in-memory sources
 * page exactly like the local server does.
 */
export function listRows(
  rows: readonly SessionExplorerSession[],
  params: SessionsParams
): SessionExplorerSessionsPage {
  const q = (params.search ?? '').trim().toLowerCase();
  const day = (r: SessionExplorerSession) =>
    r.sessionDate ?? r.startedAt.slice(0, 10);
  const matching = rows
    .filter((r) => !params.from || day(r) >= params.from)
    .filter((r) => !params.to || day(r) <= params.to)
    .filter(
      (r) =>
        !q ||
        [r.sessionId, r.sessionTitle ?? '', r.agentTool, ...r.repos, ...r.models]
          .join(' ')
          .toLowerCase()
          .includes(q)
    )
    .sort(
      (a, b) =>
        b.startedAt.localeCompare(a.startedAt) ||
        a.sessionId.localeCompare(b.sessionId)
    );
  const size = Math.min(Math.max(params.pageSize ?? 50, 1), 200);
  const offset = params.cursor ? Number(atob(params.cursor)) || 0 : 0;
  const page = matching.slice(offset, offset + size);
  const next = offset + size;
  const hasMore = next < matching.length;
  return {
    success: true,
    sessions: page,
    nextCursor: hasMore ? btoa(String(next)) : null,
    hasMore,
  };
}
