import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fileSource } from '@/data/fileSource';
import { SessionMapSourceProvider } from '@/data/SourceContext';
import { useAccount, WelcomeGate } from './AccountGate';

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } });

function stubServer({ answerPosts = true } = {}) {
  const posts: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method !== 'POST') return json({ status: 'none', email: null, enabled: true });
      posts.push(JSON.parse(String(init.body)));
      if (!answerPosts) return new Promise<Response>(() => {});
      return json({ success: true, status: 'skipped', email: null, enabled: true });
    })
  );
  return posts;
}

function App() {
  const account = useAccount('', true);
  return (
    <>
      <button type="button">Behind the gate</button>
      <WelcomeGate base="" state={account.data} />
    </>
  );
}

let root: Root | null = null;

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  document.body.innerHTML = '';
  vi.unstubAllGlobals();
});

async function renderGate(server?: { answerPosts: boolean }) {
  const posts = stubServer(server);
  const el = document.body.appendChild(document.createElement('div'));
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  root = createRoot(el);
  await act(async () => {
    root!.render(
      <QueryClientProvider client={client}>
        <SessionMapSourceProvider source={fileSource()}>
          <App />
        </SessionMapSourceProvider>
      </QueryClientProvider>
    );
  });
  await vi.waitFor(() => expect(document.activeElement?.id).toBe('welcome-email'));
  return posts;
}

const dialog = () => document.querySelector<HTMLElement>('[role="dialog"]');
const named = (text: string) =>
  [...document.querySelectorAll<HTMLElement>('a, button')].find((el) => el.textContent?.trim() === text)!;

// jsdom has no sequential focus navigation.
function pressTab() {
  const order = [
    ...document.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, summary, [tabindex]'),
  ].filter((el) => el.tabIndex >= 0 && !el.matches(':disabled'));
  const next = order[order.indexOf(document.activeElement as HTMLElement) + 1] ?? order[0];
  act(() => next.focus());
}

function press(key: string) {
  act(() => {
    document.activeElement!.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true }));
  });
}

describe('WelcomeGate', () => {
  it('opens as a labelled modal dialog and hides the page behind it', async () => {
    await renderGate();
    expect(dialog()?.getAttribute('aria-labelledby')).toBeTruthy();
    expect(document.getElementById(dialog()!.getAttribute('aria-labelledby')!)?.textContent).toBe(
      'Welcome to agentplay'
    );
    expect(named('Behind the gate').closest('[inert], [aria-hidden="true"]')).not.toBeNull();
  });

  it('wraps Tab from the last control back to the first', async () => {
    await renderGate();
    act(() => named('Continue without email').focus());
    pressTab();
    await vi.waitFor(() => expect(document.activeElement).toBe(named('Read how')));
  });

  it('skips on Escape, as "Continue without email" does, and frees the page', async () => {
    const posts = await renderGate();
    expect(posts).toEqual([]);
    press('Escape');
    await vi.waitFor(() => expect(dialog()).toBeNull());
    expect(posts).toEqual([{ action: 'skip' }]);
    expect(named('Behind the gate').closest('[inert], [aria-hidden="true"]')).toBeNull();
  });

  it('sends nothing on a second Escape while the skip is in flight', async () => {
    const posts = await renderGate({ answerPosts: false });
    press('Escape');
    await vi.waitFor(() => expect(posts).toEqual([{ action: 'skip' }]));
    press('Escape');
    await act(async () => {});
    expect(posts).toEqual([{ action: 'skip' }]);
  });

  it('stays open and sends nothing on a click outside the card', async () => {
    const posts = await renderGate();
    const overlay = dialog()!.parentElement!;
    act(() => {
      for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
        overlay.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, button: 0 }));
      }
    });
    await act(async () => {});
    expect(dialog()).not.toBeNull();
    expect(posts).toEqual([]);
  });
});
