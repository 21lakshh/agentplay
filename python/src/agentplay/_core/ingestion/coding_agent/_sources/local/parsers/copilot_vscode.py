"""Parse GitHub Copilot in VS Code (Chat / agent mode) sessions into the universal
SpanData shape.

This is a DIFFERENT product from the ``copilot`` CLI (``copilot.py`` in this
package): VS Code's built-in Copilot Chat extension, in Ask/Edit/Agent mode. It
writes one JSON document per chat session under the workspace's or the empty
window's storage:

    <appUserDir>/workspaceStorage/<ws>/chatSessions/<id>.jsonl
    <appUserDir>/globalStorage/emptyWindowChatSessions/<id>.jsonl
    <appUserDir>/globalStorage/emptyWindowChatSessions/<id>.json   (older, rarer)

``<appUserDir>`` is VS Code's (or a fork's) per-OS ``User`` directory; discovery
of the app + OS + home combinations lives in discovery, not here.

# Two on-disk shapes, one final state

A ``.json`` file is the session's final state as one document: ``{version,
sessionId, creationDate, requests: [...], inputState, ...}``.

A ``.jsonl`` file is a snapshot-plus-patches LOG of that same document, one
line per envelope ``{"kind": N, ...}``:

    kind 0  {"kind": 0, "v": <full state dict>}           -- initial snapshot
    kind 1  {"kind": 1, "k": [path...], "v": <value>}      -- set state[path] = v
    kind 2  {"kind": 2, "k": [path...], "v": [items], "i": idx?}
            -- splice state[path] (a list): "i" omitted means append "v";
               "i" present means truncate the list to "i" items then extend
               with "v" (this is also how a streamed response's text array
               grows token-by-token: repeated kind-2 patches on
               ``requests[N].response`` with a rising "i").

A later patch can rewrite an EARLIER request (e.g. a retried tool call), so
the whole file must be replayed in order every time -- there is no safe
incremental read. The replay below reproduces
the same request count, tool names, and token fields a `.json` (already-final)
sibling file shows directly.

# Per-request fields (verified against real data, not the VS Code source)

    requestId, timestamp (epoch ms), elapsedMs, isComplete, modelId (the
    router alias, e.g. "copilot/auto"), agent{extensionId, id, ...},
    result{usage{promptTokens, completionTokens}, metadata{resolvedModel,
    toolCallRounds, toolCallResults, promptTokens, outputTokens,
    renderedUserMessage, renderedGlobalContext}, errorDetails{code, message,
    isQuotaExceeded}, details (a free-text model/pricing line, e.g.
    "Claude Haiku 4.5 - 13.8 credits" -- premium-request evidence, not yet
    structured)}.

``result.usage.promptTokens`` is the primary token source (present on 14/50
real requests); ``result.metadata.{promptTokens,outputTokens}`` is checked
next (4/50) when ``usage`` is absent. Most requests carry neither -- Copilot
in VS Code will always be activity-complete but token-sparse. Every dollar
computed from what tokens ARE present must carry an "estimated" cost basis,
never treated as authoritative the way Claude's per-request usage
block is.

Model resolution fallback order:
``result.metadata.resolvedModel``, skipped if it is a routing alias -> top-level
``modelId`` (also skipped if a routing alias) -> the session-level
``inputState.selectedModel.identifier`` (last resort; not per-request, also
skipped if a routing alias). "Routing alias" means either a ``"capi-"``-prefixed
internal id, or the literal string ``"copilot/auto"`` -- present on
``modelId`` of real requests, so without also treating it
as an alias the session-level tier could never be reached.

``result.metadata.toolCallRounds[].toolCalls[]`` holds ``{id, name,
arguments}`` with ``arguments`` a JSON-ENCODED STRING (not a pre-parsed
dict). Outputs come from the per-REQUEST (not
per-round) ``result.metadata.toolCallResults[callId]``, itself a VS Code
document-model object: ``{"$mid": N, "content": [{"$mid": M, "value":
"..."}]}``. That shape carries no error flag,
so a tool call is OK when its result is present, UNSET when it never
resolved (killed, interrupted, or the map cuts off mid-round).

A per-request ``errorDetails`` (verified: e.g. ``{"code": "quotaExceeded",
"message": "..."}``) marks the whole request's Turn span as ERROR and is
NOT swallowed into a tool span.

# What is dropped, and where

``renderedUserMessage``, ``renderedGlobalContext``, ``result.details``'
free text, and every tool call/result's own content are read here to run the
same extractors every other parser runs over its transcript text
(commit/PR/ticket detection via ``produced.py``, edit evidence via
``code_authorship.py``) -- exactly like Claude Code, Codex, Cursor and
opencode transcripts, which also carry real prompt/tool text into
``SpanData.attributes`` internally. NONE of that text is unique to this
parser's output.

# Known gap: no cwd / repo resolution

VS Code does not store the workspace path inside the chat session document
itself -- it lives in a sibling ``workspaceStorage/<ws>/workspace.json``
this parser does not read. Sessions from this parser therefore carry no
``cwd`` and resolve no repo/PR-repo context until that sibling file is read
and threaded through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_KIND_JSON_DOC,
    as_count,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    creation_output_attributes,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

_TOOL_ALIASES: dict[str, str] = aliases_for("copilot-vscode")

# Internal routing aliases VS Code stamps on `modelId`/`resolvedModel` instead
# of a model a person would recognise -- never a real billable model.
_ROUTING_MODEL_PREFIX = "capi-"
# The router alias `modelId` appears on real requests -- also never a real model, but not `capi-`-prefixed, so
# it needs its own check wherever a routing alias is filtered.
_ROUTING_MODEL_ALIAS = "copilot/auto"


def _is_routing_alias(model: str | None) -> bool:
    return bool(model) and (
        model.startswith(_ROUTING_MODEL_PREFIX) or model == _ROUTING_MODEL_ALIAS
    )


def parse_copilot_vscode(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse one VS Code Copilot chat session (``.json`` or replayed ``.jsonl``)."""
    state = _load_state(data)
    session_model = _session_level_model(state)
    requests = (state or {}).get("requests")
    if not isinstance(requests, list):
        requests = []
    session_id = (state or {}).get("sessionId") or trace_id
    session_created = _epoch_ms((state or {}).get("creationDate"))

    if not requests:
        now = session_created or datetime.now(UTC)
        trace = TraceData(trace_id=trace_id, start_time=now, end_time=now, spans=[])
        return trace, []

    all_spans: list[SpanData] = []
    root_id = f"{trace_id}:root"
    turn_starts: list[datetime] = []
    turn_ends: list[datetime] = []

    for turn_num, req in enumerate(requests, 1):
        if not isinstance(req, dict):
            continue
        turn_start = _epoch_ms(req.get("timestamp")) or session_created or datetime.now(UTC)
        elapsed = req.get("elapsedMs")
        turn_end = (
            turn_start + _ms_delta(elapsed) if isinstance(elapsed, int | float) else turn_start
        )
        turn_starts.append(turn_start)
        turn_ends.append(turn_end)

        result = req.get("result") if isinstance(req.get("result"), dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        error = result.get("errorDetails") if isinstance(result.get("errorDetails"), dict) else {}

        model = _resolved_model(metadata, req, session_model)
        turn_input = _rendered_user_message_text(metadata.get("renderedUserMessage"))
        turn_output = _response_text(req.get("response"))
        prompt_tokens = usage.get("promptTokens")
        completion_tokens = usage.get("completionTokens")
        if prompt_tokens is None:
            prompt_tokens = metadata.get("promptTokens")
        if completion_tokens is None:
            completion_tokens = metadata.get("outputTokens") or metadata.get("completionTokens")

        request_id = req.get("requestId") or f"{session_id}:{turn_num}"
        turn_id = f"{trace_id}:turn:{turn_num}"
        turn_span = SpanData(
            span_id=turn_id,
            trace_id=trace_id,
            parent_id=root_id,
            name=f"Turn {turn_num}",
            span_kind="CHAIN",
            status_code="ERROR" if error else "OK",
            start_time=turn_start,
            end_time=turn_end,
            attributes={
                "llm.model_name": model or "",
                "input.value": turn_input[:2000],
                "output.value": turn_output[:2000],
                "llm.token_count.prompt": as_count(prompt_tokens),
                "llm.token_count.completion": as_count(completion_tokens),
                # No cache/cache-write signal exists for this agent (see module
                # docstring): tokens above are approximate, never authoritative.
                "harness.request_id": str(request_id),
                "harness.is_complete": bool(req.get("isComplete")),
                # Free-text pricing/model line (e.g. "Claude Haiku 4.5 - 13.8
                # credits"): premium-request evidence, not yet structured.
                "copilot.request_detail": str(result.get("details") or "")[:200],
                **(
                    {
                        "error.code": str(error.get("code") or "")[:80],
                        "error.message": str(error.get("message") or "")[:300],
                    }
                    if error
                    else {}
                ),
            },
        )
        turn_span.attributes.update(
            source_attrs({}, kind=SOURCE_KIND_JSON_DOC, row_cursor=str(request_id))
        )
        all_spans.append(turn_span)

        tool_call_results = metadata.get("toolCallResults")
        tool_call_results = tool_call_results if isinstance(tool_call_results, dict) else {}
        rounds = metadata.get("toolCallRounds")
        for round_ in rounds if isinstance(rounds, list) else []:
            for tc in _tool_calls_of(round_):
                span = _tool_span(tc, tool_call_results, trace_id, turn_id, turn_start, turn_end)
                if span is not None:
                    all_spans.append(span)

    session_start = min(turn_starts) if turn_starts else session_created or datetime.now(UTC)
    session_end = max(turn_ends) if turn_ends else session_start
    root_span = SpanData(
        span_id=root_id,
        trace_id=trace_id,
        parent_id=None,
        name="Session",
        span_kind="AGENT",
        status_code="OK",
        start_time=session_start,
        end_time=session_end,
        attributes={
            "session.id": session_id,
            "session.agent_tool": "copilot-vscode",
            "project.name": project_name or "",
            "user.id": user_id or "",
        },
    )
    all_spans.insert(0, root_span)

    trace = TraceData(
        trace_id=trace_id, start_time=session_start, end_time=session_end, spans=all_spans
    )
    return trace, all_spans


def _tool_calls_of(round_: object) -> list[dict]:
    calls = (round_ or {}).get("toolCalls") if isinstance(round_, dict) else None
    return [c for c in calls if isinstance(c, dict)] if isinstance(calls, list) else []


def _tool_span(
    tc: dict,
    tool_call_results: dict,
    trace_id: str,
    turn_id: str,
    turn_start: datetime,
    turn_end: datetime,
) -> SpanData | None:
    call_id = tc.get("id") if isinstance(tc.get("id"), str) else None
    raw_name = tc.get("name")
    if not isinstance(raw_name, str) or not raw_name:
        return None
    name = _TOOL_ALIASES.get(raw_name, raw_name)

    args = tc.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, RecursionError):
            args = {}
    if not isinstance(args, dict):
        args = {}
    args = _normalize_file_path_arg(args)
    tool_input = json.dumps(args) if args else ""

    result_obj = tool_call_results.get(call_id) if isinstance(call_id, str) else None
    has_result = call_id is not None and call_id in tool_call_results
    output_text = _tool_result_text(result_obj)

    command_attrs = creation_command_attributes(tool_input, name)
    output_attrs = creation_output_attributes(
        output_text,
        command_attrs.get("pr.create.provider"),
        command_attrs.get("pr.create.repository"),
    )
    edit_evidence = extract_edit_evidence(name, tool_input, successful=has_result)

    return SpanData(
        span_id=f"{trace_id}:tool:{call_id or name}:{id(tc)}",
        trace_id=trace_id,
        parent_id=turn_id,
        name=name,
        span_kind="TOOL",
        status_code="OK" if has_result else "UNSET",
        start_time=turn_start,
        end_time=turn_end,
        attributes={
            "tool.name": name,
            "input.value": tool_input[:2000],
            "output.value": output_text[:2000],
            "code.edit_evidence": edit_evidence,
            **command_attrs,
            **output_attrs,
        },
    )


def _tool_result_text(result_obj: object) -> str:
    """Flatten a VS Code tool-result document (``{"content": [{"value": ...}]}``)
    or a plain string into the text other extractors expect."""
    if isinstance(result_obj, str):
        return result_obj
    if isinstance(result_obj, dict):
        content = result_obj.get("content")
        if isinstance(content, list):
            parts = [c.get("value") for c in content if isinstance(c, dict)]
            return "\n".join(p for p in parts if isinstance(p, str))
    return ""


def _rendered_user_message_text(parts: object) -> str:
    """The user's typed prompt, as ``result.metadata.renderedUserMessage`` carries
    it: a list of parts, only the plain-text ones (``type == 1``) carrying a string ``text`` field. Image/other reference parts
    (``type`` 2 and 3) never have one and are skipped, not stringified.

    Without this, every Copilot VS Code turn read as an empty prompt to every
    text-based extractor.
    """
    if not isinstance(parts, list):
        return ""
    texts = [p.get("text") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    return "\n".join(texts)


def _response_text(parts: object) -> str:
    """The assistant's final response text, as ``request.response`` carries it: a
    list of heterogeneous parts (thinking, tool-invocation markers, markdown,
    ...). Only the markdown/text parts -- verified real shape: no ``kind`` key at
    all, plus a string ``value`` -- are joined; ``thinking`` parts (the model's
    own chain-of-thought, NOT the answer) are deliberately excluded, same as
    every other kind (tool markers, edit groups, confirmations, ...)."""
    if not isinstance(parts, list):
        return ""
    texts = [
        p.get("value")
        for p in parts
        if isinstance(p, dict) and p.get("kind") is None and isinstance(p.get("value"), str)
    ]
    return "\n".join(texts)


def _normalize_file_path_arg(args: dict) -> dict:
    """VS Code tool args carry ``filePath`` (camelCase); the shared
    ``file_paths.py`` extractors, and ``code_authorship.extract_edit_evidence``'s
    ``path = obj.get("file_path")``, only ever match snake_case. `file_path`
    key goes FIRST so it survives `input.value`'s 2000-char truncation on a
    large edit -- same reasoning as opencode's `_normalize_file_path_arg`.
    """
    if "file_path" in args:
        return args
    file_path = args.get("filePath")
    if isinstance(file_path, str) and file_path:
        return {"file_path": file_path, **args}
    return args


def _resolved_model(metadata: dict, req: dict, session_model: str | None) -> str | None:
    resolved = model_id_or_none(metadata.get("resolvedModel"))
    if resolved and not _is_routing_alias(resolved):
        return resolved
    top = model_id_or_none(req.get("modelId"))
    if top and not _is_routing_alias(top):
        return top
    # Last resort: the session-level model the user had selected, not per-request
    # (verified real shape: state.inputState.selectedModel.identifier) -- still
    # better than a bare routing alias when neither per-request field resolved
    # to one. `top` is "copilot/auto" on every real request (verified), so
    # without this tier the session-level signal could never be reached.
    if session_model and not _is_routing_alias(session_model):
        return session_model
    # Every tier was an alias: prefer `top` ("copilot/auto", a recognisable
    # label) over `resolved` (a capi-* internal pool id, meaningless to a
    # person) over session_model, rather than the tier order above -- that
    # order exists to prefer a REAL model where one exists, not to rank
    # unhelpful aliases against each other.
    return top or resolved or session_model


def _session_level_model(state: dict | None) -> str | None:
    input_state = (state or {}).get("inputState")
    selected = (input_state or {}).get("selectedModel") if isinstance(input_state, dict) else None
    if not isinstance(selected, dict):
        return None
    return model_id_or_none(selected.get("identifier"))


def _epoch_ms(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _ms_delta(ms: float) -> timedelta:
    return timedelta(milliseconds=ms)


def _load_state(data: bytes) -> dict | None:
    """Return the session's final ``{requests: [...], ...}`` document.

    A ``.json`` file IS that document. A ``.jsonl`` file is a snapshot (kind 0)
    plus patches (kind 1 set / kind 2 list-splice); replay every line in order,
    since a later patch can rewrite an earlier request. Malformed lines and
    patches that reference a path no longer present are skipped, not fatal --
    partial reconstruction beats losing the whole session.
    """
    try:
        doc = json.loads(data)
    except (ValueError, RecursionError):
        doc = None
    if isinstance(doc, dict) and "requests" in doc:
        return doc

    state: dict | None = None
    for raw_line in data.split(b"\n"):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("kind")
        if kind == 0:
            v = obj.get("v")
            state = v if isinstance(v, dict) else {}
            continue
        if state is None or kind not in (1, 2):
            continue
        path = obj.get("k") or []
        if not isinstance(path, list) or not path:
            continue
        cur = state
        ok = True
        for key in path[:-1]:
            try:
                cur = cur[key]
            except (KeyError, IndexError, TypeError):
                ok = False
                break
        if not ok:
            continue
        last = path[-1]
        if kind == 1:
            try:
                cur[last] = obj.get("v")
            except (TypeError, IndexError):
                pass
            continue
        # kind == 2: splice a list. No "i" means append; "i" present means
        # truncate to that length, then extend (how streamed response text
        # arrays grow -- verified against real ["requests", N, "response"]
        # patches with a monotonically rising "i").
        try:
            target = cur[last]
        except (KeyError, IndexError, TypeError):
            continue
        if not isinstance(target, list):
            continue
        values = obj.get("v")
        values = values if isinstance(values, list) else []
        idx = obj.get("i")
        if idx is None:
            target.extend(values)
        elif isinstance(idx, int):
            del target[idx:]
            target.extend(values)
    return state


__all__ = ["parse_copilot_vscode"]
