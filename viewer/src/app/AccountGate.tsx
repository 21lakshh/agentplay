import { useState, type FormEvent } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EnvelopeSimple, LockSimple } from '@phosphor-icons/react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import {
  AccountError,
  fetchAccount,
  isLocalBase,
  postAccount,
  showsWelcome,
  type AccountAction,
  type AccountState,
} from '@/data/account';
import { useSessionExplorerStatus } from '@/data/hooks';
import { researchUrl } from '@/core/entelligence';

const key = (base: string) => ['account', base] as const;

/** The signup state from the local server; off unless `enabled` and the
 * server is on this machine (see `isLocalBase`). */
export function useAccount(base: string, enabled: boolean) {
  return useQuery({
    queryKey: key(base),
    queryFn: () => fetchAccount(base),
    enabled: enabled && isLocalBase(base),
    staleTime: Infinity,
    retry: false,
  });
}

function useAccountAction(base: string) {
  const qc = useQueryClient();
  return useMutation<AccountState, AccountError, AccountAction>({
    mutationFn: (body) => postAccount(base, body),
    onSuccess: (state) => qc.setQueryData(key(base), state),
  });
}

/**
 * First-run card over the viewer. The user's sessions keep loading behind it,
 * so the first thing they see when it closes is their own data.
 */
export function WelcomeGate({ base, state }: { base: string; state: AccountState | undefined }) {
  const [email, setEmail] = useState('');
  const act = useAccountAction(base);
  const status = useSessionExplorerStatus();
  if (!showsWelcome(state)) return null;

  const count = status.data?.mapCount ?? 0;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    act.mutate({ action: 'signup', email: email.trim() });
  };
  const busy = act.isPending;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-ds-surface-white/70 p-4 pt-[15vh] backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="welcome-title"
        className="w-full max-w-md rounded-xl border border-ds-border-subtlest bg-ds-surface-white p-6 shadow-lg"
      >
        <img src="entelligence-logo.svg" alt="" className="mb-4 size-9" />
        <h1 id="welcome-title" className="ds-text-title-md-semibold text-ds-text-darker">
          {count > 0 ? `Your ${count} ${count === 1 ? 'session is' : 'sessions are'} ready` : 'Welcome to agentplay'}
        </h1>
        <p className="mt-1.5 ds-text-body-sm-medium text-ds-text-default">
          agentplay is the first step toward coding agents that learn from their own mistakes.{' '}
          <a
            href={researchUrl('welcome')}
            target="_blank"
            rel="noopener noreferrer"
            className="text-ds-brand-subtle underline underline-offset-2 hover:text-ds-text-darker"
          >
            Read how
          </a>
          , and get updates on Entelligence for you and your team.
        </p>
        <form onSubmit={submit} className="mt-4 flex flex-col gap-2">
          <label htmlFor="welcome-email" className="sr-only">
            Email
          </label>
          <div className="flex gap-2">
            <Input
              id="welcome-email"
              type="email"
              autoComplete="email"
              autoFocus
              required
              placeholder="you@company.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              aria-invalid={act.isError || undefined}
              aria-describedby={act.isError ? 'welcome-error' : 'welcome-privacy'}
              disabled={busy}
            />
            <Button type="submit" disabled={busy || !email.trim()} className="shrink-0">
              {busy ? 'Saving…' : 'Get updates'}
            </Button>
          </div>
          {act.isError ? (
            <p id="welcome-error" role="alert" className="ds-text-body-xs-medium text-ds-danger-default">
              {act.error.message}
            </p>
          ) : null}
        </form>
        <details id="welcome-privacy" className="mt-3 ds-text-body-xs-medium text-ds-text-subtle">
          <summary className="flex cursor-pointer list-none items-center gap-1.5 [&::-webkit-details-marker]:hidden">
            <LockSimple size={12} className="shrink-0" aria-hidden />
            <span>
              Nothing from your sessions leaves this machine.{' '}
              <span className="underline underline-offset-2">What&rsquo;s sent?</span>
            </span>
          </summary>
          <p className="mt-1.5 pl-[18px]">
            Signing up sends your email, a random install id, the agentplay version and the time. Your sessions,
            prompts and code never leave this machine. Disconnect any time from the top bar.
          </p>
        </details>
        <div className="mt-5 flex justify-end">
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-ds-text-subtle"
            disabled={busy}
            onClick={() => act.mutate({ action: 'skip' })}
          >
            Continue without email
          </Button>
        </div>
      </div>
    </div>
  );
}

/** Top-bar entry: who you are and the way back out, or, after a skip, the way
 * back in. Reopening only changes the cached state; nothing is stored until
 * the card is answered again. */
export function AccountChip({ base, state }: { base: string; state: AccountState | undefined }) {
  const act = useAccountAction(base);
  const qc = useQueryClient();
  if (state?.enabled && state.status === 'skipped') {
    return (
      <Button
        variant="ghost"
        size="sm"
        className="h-7 gap-1.5 px-2 text-ds-text-subtle"
        onClick={() => qc.setQueryData(key(base), { ...state, status: 'none' })}
      >
        <EnvelopeSimple size={14} aria-hidden />
        Get updates
      </Button>
    );
  }
  if (state?.status !== 'signed_up' || !state.email) return null;
  return (
    <Popover>
      <PopoverTrigger
        render={
          <Button variant="ghost" size="sm" className="h-7 gap-1.5 px-2 text-ds-text-subtle">
            <EnvelopeSimple size={14} aria-hidden />
            <span className="max-w-48 truncate">{state.email}</span>
          </Button>
        }
      />
      <PopoverContent align="end" className="w-72 p-3">
        <p className="ds-text-body-xs-medium text-ds-text-default">
          Signed up as <span className="ds-text-body-xs-semibold">{state.email}</span>. Disconnecting removes it
          from this machine and sends Entelligence one request to delete it, with the same details. It is sent once and
          not retried, so if we can't be reached right then, it may not arrive and the email may stay on our list.
        </p>
        <Button
          variant="outline"
          size="sm"
          className="mt-3 w-full"
          disabled={act.isPending}
          onClick={() => act.mutate({ action: 'disconnect' })}
        >
          {act.isPending ? 'Disconnecting…' : 'Disconnect'}
        </Button>
      </PopoverContent>
    </Popover>
  );
}
