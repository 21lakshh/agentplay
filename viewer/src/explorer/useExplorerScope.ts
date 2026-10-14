import { useCallback, useEffect, useMemo } from 'react';
import { useQueryStates } from 'nuqs';
import type { SessionMap } from '@/core/schema';
import { agentLabel, deriveLensDoc, subagentOrdinal } from '@/core/sessionMapModel';
import { orderedSubagents, taskSummaries } from '@/core/sessionLayout';
import {
  CLEAR_SCOPE_PARAMS,
  SCOPE_PARAMS,
  deriveScopeDoc,
  modelUsage,
  normalizeScope,
  scopeForSession,
  sessionTasks,
  stampTasks,
  withoutSrcSeq,
  type Scope,
} from '@/core/sessionScope';

export type SetScope = (
  next: Scope | ((prev: Scope) => Scope),
  /** also close the agent lens (an error jump may target another agent) */
  opts?: { clearLens?: boolean }
) => void;

const isEmptyScope = (s: Scope, sub: string | null) =>
  s.task == null && s.model == null && sub == null;

/** The agent lens (`?sub=<agentId>`): an id this session does not have reads
 * as the main agent. */
function resolveLens(map: SessionMap, subParam: string | null): string {
  if (!subParam || subParam === 'main') return 'main';
  return map.agents.some((a) => a.id === subParam) ? subParam : 'main';
}

/**
 * The agent lens and the scope (task window,
 * model), all in the URL: the full map is task-stamped, scoped
 * via `deriveScopeDoc`, then lensed via `deriveLensDoc`. `key` changes with
 * any of them so the replay body remounts (its lazy playback init must
 * re-run against the new event count).
 */
export function useExplorerScope(sessionId: string, rawMap: SessionMap) {
  // `srcSeq` is the viewer's own; one carried in by the document is dropped
  // here, before any derivation could pass it on (`withoutSrcSeq`).
  const fullMap = useMemo(() => stampTasks(withoutSrcSeq(rawMap)), [rawMap]);
  // Scope lives in the URL so reload, share and Back keep it.
  const [params, setParams] = useQueryStates(SCOPE_PARAMS);
  // Scope is bound to the session it was set on, so a search or window
  // change that swaps the effective session does not carry the old scope
  // over; a stale scope reads empty here and is cleared from the URL.
  const {
    scope: urlScope,
    sub: subParam,
    stale,
    unowned,
  } = useMemo(() => scopeForSession(params, sessionId), [params, sessionId]);
  const ordinals = useMemo(() => subagentOrdinal(fullMap), [fullMap]);
  const tasks = useMemo(() => sessionTasks(fullMap).tasks, [fullMap]);
  const models = useMemo(() => modelUsage(fullMap), [fullMap]);
  // A task or model this session does not have reads as unset (like `?sub=`).
  const scope = useMemo(
    () => normalizeScope(urlScope, tasks, models),
    [urlScope, tasks, models]
  );
  const lens = resolveLens(fullMap, subParam);
  const sub = lens === 'main' ? null : lens;
  useEffect(() => {
    if (stale) void setParams(CLEAR_SCOPE_PARAMS);
    // an unowned deep link belongs to the first session it is shown with
    else if (unowned) void setParams({ ss: sessionId });
    // a stale task / model is dropped from the URL (replace, no history entry)
    else if (scope !== urlScope)
      void setParams({
        task: scope.task,
        model: scope.model,
        ss: isEmptyScope(scope, sub) ? null : sessionId,
      });
  }, [stale, unowned, scope, urlScope, sub, sessionId, setParams]);

  const setScope = useCallback<SetScope>(
    (next, opts) => {
      const s1 = typeof next === 'function' ? next(scope) : next;
      // An agent with nothing in the new scope would land on an empty replay:
      // close its lens (the filter that was just changed wins).
      const dropLens =
        sub != null &&
        (opts?.clearLens ||
          !deriveScopeDoc(fullMap, s1).events.some((e) => e.agent === sub));
      void setParams({
        task: s1.task,
        model: s1.model,
        ...(dropLens ? { sub: null } : {}),
        ss: isEmptyScope(s1, dropLens ? null : sub) ? null : sessionId,
      });
    },
    [scope, setParams, sessionId, sub, fullMap]
  );
  const setLens = useCallback(
    (id: string) => {
      const nextSub = id === 'main' ? null : id;
      void setParams({
        sub: nextSub,
        ss: isEmptyScope(scope, nextSub) ? null : sessionId,
      });
    },
    [scope, setParams, sessionId]
  );

  const clearAll = useCallback(
    () => void setParams(CLEAR_SCOPE_PARAMS),
    [setParams]
  );

  const subs = useMemo(
    () => orderedSubagents(fullMap, ordinals),
    [fullMap, ordinals]
  );
  const taskSums = useMemo(() => taskSummaries(fullMap), [fullMap]);
  const scoped = useMemo(
    () => deriveScopeDoc(fullMap, scope),
    [fullMap, scope]
  );
  const map = useMemo(() => {
    // A lens on an agent with nothing in this scope shows an empty replay,
    // not the whole scope (deriveLensDoc falls back to its input).
    if (lens !== 'main' && !scoped.events.some((e) => e.agent === lens))
      return { ...scoped, events: [], agents: [], prs: [] };
    return deriveLensDoc(scoped, lens);
  }, [scoped, lens]);
  const lensLabel =
    lens === 'main' ? null : agentLabel(fullMap, lens, ordinals);
  const key = `${lens}|${scope.task ?? ''}|${scope.model ?? ''}`;
  return {
    key,
    fullMap,
    scopedMap: scoped,
    map,
    scope,
    setScope,
    lens,
    sub,
    setLens,
    clearAll,
    lensLabel,
    ordinals,
    tasks,
    models,
    subs,
    taskSums,
  };
}

export type ExplorerScope = ReturnType<typeof useExplorerScope>;
