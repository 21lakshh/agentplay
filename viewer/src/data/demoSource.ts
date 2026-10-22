import {
  sessionExplorerSessionSchema,
  type SessionExplorerSession,
} from '@/core/schema';
import { SessionExplorerError } from './errors';
import { listRows } from './memoryListing';
import { parseMapBytes } from './parse';
import type { SessionMapSource } from './source';

type DemoRow = SessionExplorerSession & { file?: string };

/**
 * The bundled synthetic sessions: `demo/index.json` (`{sessions: [row +
 * file]}`) and one `demo/<file>` map document per row, as static files next
 * to the page. `root` is resolved against the page, so the build works from
 * any path.
 */
export function demoSource(root = 'demo/'): SessionMapSource {
  const base = new URL(root, document.baseURI);
  let index: Promise<DemoRow[]> | null = null;
  const load = () =>
    (index ??= fetch(new URL('index.json', base))
      .then((r) => {
        if (!r.ok) throw new SessionExplorerError('demo index missing', 'not_found');
        return r.json() as Promise<{ sessions?: unknown[] }>;
      })
      .then((body) =>
        (body.sessions ?? []).map((raw) => {
          const row = sessionExplorerSessionSchema.parse(raw) as DemoRow;
          return row;
        })
      )
      .catch((err) => {
        index = null;
        throw err;
      }));
  return {
    id: 'demo',
    label: 'Demo sessions',
    async status() {
      const rows = await load();
      const latest = rows.map((r) => r.sessionDate ?? '').sort().pop() || null;
      return { success: true, available: rows.length > 0, mapCount: rows.length, latestSessionDate: latest, scanning: false };
    },
    async sessions(params) {
      return listRows(await load(), params);
    },
    async maps(ids) {
      const rows = await load();
      return {
        success: true,
        expiresIn: 3600,
        maps: ids.map((id) => {
          const row = rows.find((r) => r.sessionId === id);
          return row?.file
            ? { sessionId: id, url: new URL(row.file, base).href, schemaVersion: 1, contentHash: row.contentHash ?? id, bytesGz: row.bytesGz }
            : { sessionId: id, status: 'not_found' as const };
        }),
      };
    },
    async fetchMap(meta) {
      const r = await fetch(meta.url);
      if (!r.ok) throw new SessionExplorerError(`demo map ${r.status}`, 'not_found');
      return parseMapBytes(new Uint8Array(await r.arrayBuffer()));
    },
  };
}
