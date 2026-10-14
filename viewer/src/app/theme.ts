import { useCallback, useEffect, useState } from 'react';

type Theme = 'light' | 'dark';

declare global {
  interface Window {
    __agentplayApplyTheme?: () => void;
  }
}

const KEY = 'agentplay.theme';
const current = (): Theme =>
  document.documentElement.classList.contains('dark') ? 'dark' : 'light';

/** The resolved theme plus a toggle that pins the other one. The pin lives
 * in localStorage (a per-viewer convenience; every access is guarded). */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(current);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(current()));
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    return () => obs.disconnect();
  }, []);
  const toggle = useCallback(() => {
    const next: Theme = current() === 'dark' ? 'light' : 'dark';
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // storage blocked: the toggle still applies for this page view
    }
    const url = new URL(window.location.href);
    if (url.searchParams.has('theme')) {
      url.searchParams.delete('theme');
      window.history.replaceState(window.history.state, '', url);
    }
    if (window.__agentplayApplyTheme) window.__agentplayApplyTheme();
    document.documentElement.classList.toggle('dark', next === 'dark');
  }, []);
  return { theme, toggle };
}
