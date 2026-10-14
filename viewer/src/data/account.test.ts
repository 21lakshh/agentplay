import { describe, expect, it } from 'vitest';
import { fetchAccount, isLocalBase, parseAccount, postAccount, showsWelcome } from './account';

describe('parseAccount', () => {
  it('reads a well-formed answer', () => {
    expect(parseAccount({ success: true, status: 'signed_up', email: 'a@b.co', enabled: true })).toEqual({
      status: 'signed_up',
      email: 'a@b.co',
      enabled: true,
    });
  });

  it('treats anything malformed as nothing to offer', () => {
    for (const body of [null, 'x', {}, { status: 'weird', enabled: 'yes' }]) {
      expect(parseAccount(body)).toEqual({ status: 'none', email: null, enabled: false });
    }
  });
});

describe('showsWelcome', () => {
  it('shows only when signup is on and nothing was chosen yet', () => {
    expect(showsWelcome({ status: 'none', email: null, enabled: true })).toBe(true);
    expect(showsWelcome({ status: 'none', email: null, enabled: false })).toBe(false);
    expect(showsWelcome({ status: 'skipped', email: null, enabled: true })).toBe(false);
    expect(showsWelcome({ status: 'signed_up', email: 'a@b.co', enabled: true })).toBe(false);
    expect(showsWelcome(undefined)).toBe(false);
  });
});

describe('isLocalBase', () => {
  it('allows same-origin and this machine only', () => {
    for (const ok of ['', 'http://127.0.0.1:7317', 'http://localhost:5173/', 'http://[::1]:7317']) {
      expect(isLocalBase(ok)).toBe(true);
    }
    for (const no of ['https://evil.example', 'http://127.0.0.1.evil.example', 'javascript:alert(1)', 'not a url']) {
      expect(isLocalBase(no)).toBe(false);
    }
  });

  it('never sends an email to a remote ?server base', async () => {
    let called = false;
    const real = globalThis.fetch;
    globalThis.fetch = (async () => {
      called = true;
      return new Response('{}');
    }) as typeof fetch;
    try {
      await expect(postAccount('https://evil.example', { action: 'signup', email: 'a@b.co' })).rejects.toThrow();
      expect(await fetchAccount('https://evil.example')).toEqual({ status: 'none', email: null, enabled: false });
    } finally {
      globalThis.fetch = real;
    }
    expect(called).toBe(false);
  });
});
