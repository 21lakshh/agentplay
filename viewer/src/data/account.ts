/**
 * The optional email signup, through the local server (`GET`/`POST
 * /api/account`). The viewer itself still talks only to the local server: it
 * is the server that forwards an email, and only one the user typed in.
 */

export type AccountStatus = 'none' | 'signed_up' | 'skipped';

export interface AccountState {
  status: AccountStatus;
  email: string | null;
  /** False when no endpoint is set or the user opted out (DO_NOT_TRACK). */
  enabled: boolean;
}

export type AccountAction =
  | { action: 'signup'; email: string }
  | { action: 'skip' }
  | { action: 'disconnect' };

export class AccountError extends Error {
  constructor(
    message: string,
    readonly code: string
  ) {
    super(message);
  }
}

const STATUSES: readonly AccountStatus[] = ['none', 'signed_up', 'skipped'];

/** Anything unexpected reads as "nothing to offer": the card must never
 * block the viewer because of a malformed answer. */
export function parseAccount(body: unknown): AccountState {
  const b = (body && typeof body === 'object' ? body : {}) as Record<string, unknown>;
  const status = STATUSES.includes(b.status as AccountStatus) ? (b.status as AccountStatus) : 'none';
  return {
    status,
    email: typeof b.email === 'string' ? b.email : null,
    enabled: b.enabled === true,
  };
}

const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);

/**
 * Whether account calls may go to `base`: same-origin (`''`, the CLI serving
 * this page) or a server on this machine. `?server=` can name any host, and
 * this is the one call that carries something the user typed, so it never
 * leaves the machine from the browser.
 */
export function isLocalBase(base: string): boolean {
  if (base === '') return true;
  try {
    const u = new URL(base);
    return (u.protocol === 'http:' || u.protocol === 'https:') && LOCAL_HOSTS.has(u.hostname);
  } catch {
    return false;
  }
}

function url(base: string): string {
  if (!isLocalBase(base)) throw new AccountError('Signup only talks to the agentplay server on this machine.', 'not_local');
  return `${base.replace(/\/+$/, '')}/api/account`;
}

export async function fetchAccount(base: string): Promise<AccountState> {
  if (!isLocalBase(base)) return { status: 'none', email: null, enabled: false };
  const r = await fetch(url(base), { headers: { Accept: 'application/json' } });
  if (!r.ok || !(r.headers.get('content-type') ?? '').includes('json')) {
    return { status: 'none', email: null, enabled: false };
  }
  return parseAccount(await r.json());
}

export async function postAccount(base: string, body: AccountAction): Promise<AccountState> {
  const r = await fetch(url(base), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  let answer: Record<string, unknown> = {};
  try {
    answer = (await r.json()) as Record<string, unknown>;
  } catch {
    // keep the status-derived error below
  }
  if (!r.ok) {
    throw new AccountError(
      typeof answer.error === 'string' ? answer.error : 'Something went wrong. Try again.',
      typeof answer.code === 'string' ? answer.code : 'internal_error'
    );
  }
  return parseAccount(answer);
}

/** Whether the welcome card should cover the viewer. */
export function showsWelcome(state: AccountState | undefined): boolean {
  return !!state && state.enabled && state.status === 'none';
}
