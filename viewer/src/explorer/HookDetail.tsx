import { Skeleton } from '@/components/ui/skeleton';
import { useHookDetail } from '@/data/hooks';

/** What a failed hook said and ran. A map never carries this text, so it is
 * read from the transcript on demand and only on the recording machine. */
export function HookDetail({
  sessionId,
  uuid,
}: {
  sessionId: string;
  uuid: string;
}) {
  const query = useHookDetail(sessionId, uuid);
  if (query.isPending && query.fetchStatus !== 'idle')
    return (
      <div className="mt-2 flex flex-col gap-1" aria-hidden>
        <Skeleton className="h-3 w-full" />
        <Skeleton className="h-3 w-2/3" />
      </div>
    );
  const { message, command } = query.data ?? {};
  if (!message && !command)
    return (
      <p className="mt-2 ds-text-body-xs-medium text-ds-text-subtler">
        Details stay on the machine that recorded the session.
      </p>
    );
  return (
    <div className="mt-2 flex flex-col gap-1.5 ds-text-body-xs-medium">
      {message ? (
        <div>
          <div className="text-ds-text-subtle">message</div>
          <p className="max-h-24 overflow-y-auto whitespace-pre-wrap break-words text-ds-text-darker">
            {message}
          </p>
        </div>
      ) : null}
      {command ? (
        <div>
          <div className="text-ds-text-subtle">command</div>
          <code className="block break-all font-mono text-ds-text-darker">
            {command}
          </code>
        </div>
      ) : null}
    </div>
  );
}
