"""Parse a synthesized Kiro NDJSON transcript into the universal SpanData shape.

Kiro persists a session as a DIRECTORY, not a file:
``~/.kiro/sessions/<ws-hash>/<session-id>/`` holds ``session.json`` +
``messages.jsonl``, with sub-agent runs in ``sub-executions/<exec-id>.jsonl``.
Materialization merges those into one NDJSON transcript per session - a header
line, then every event verbatim in timestamp order (see
``agentplay.materialize.find_kiro_candidates``):

    {"type": "kiro_session", "id": ..., "title": ..., "agentMode": "spec"|"vibe",
     "workspacePaths": [...], "createdAt": <ISO>, "lastModifiedAt": <ISO>,
     "autopilot": bool, "specWorkflow": ..., "status": ...}
    {"id": ..., "timestamp": <ISO>, "payload": {"type": "user", "content": ...}}
    {"id": ..., "timestamp": <ISO>, "payload": {"type": "assistant", "content": ...}}
    {"id": ..., "timestamp": <ISO>, "payload": {"type": "tool_call", "toolCallId": ...,
     "toolName": ..., "args": {...}, "status": ..., "kind": ...}}
    {"id": ..., "timestamp": <ISO>, "payload": {"type": "tool_result", "toolCallId": ...,
     "content": ..., "success": bool}}
    ...

Unlike Cursor's synthesized format, every Kiro event carries a real ISO
timestamp, so span times are the true wall clock — no synthetic spread needed.

# Turn boundaries

``user`` events open a turn; ``turn_end`` closes the execution and carries
``stopReason``. Sub-execution files contain only ``assistant``/``tool_call``/
``tool_result`` (never ``user``), so merging them inline can't fabricate turns —
their tool calls land in the enclosing turn, which is what we want: a sub-agent's
work belongs to the turn that spawned it.

# Tokens and cost

Kiro bills in CREDITS, not tokens: ``usage_summary`` events carry
``promptTurnSummaries[].usage`` with ``unit: "credit"``, and no prompt/completion
counts exist anywhere in the transcript. So per-turn token attrs stay 0 (same as
Cursor's no-native-usage default) and the session token/cost attrs are
deliberately NOT stamped - an absent key means "no data" when spans are
aggregated, whereas a 0 would assert the session genuinely billed nothing.
The credit total lands on the root span as ``session.kiro.total_credits``
for a future credit→USD conversion; nothing consumes it yet.

Kiro records no model name either, so ``llm.model_name`` is "" throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._paths import basename as path_basename
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    parse_jsonl_records,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching every other local parser.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("kiro")


def parse_kiro(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a synthesized Kiro NDJSON transcript into a span tree.

    Args:
        data: raw NDJSON bytes — `kiro_session` header line, then event lines.
        trace_id: the source-prefixed session id (e.g. `local:kiro:<session-id>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` (from the header's
            `workspacePaths[0]`); derived from that path here when not given.

    Returns:
        `(trace, all_spans)` with the trace's `spans` matching the list. Empty
        when the header is missing/malformed or there are no events —
        `LocalSource` already skips indexing such a file (see
        `_index_kiro_file`), so this is a defensive fallback for direct callers.
    """
    header, events = _parse_ndjson(data)
    if header is None or not events:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    cwd = _header_cwd(header)
    if not project_name and cwd:
        project_name = path_basename(cwd) or None

    session_start = _iso_to_dt(header.get("createdAt"))
    session_end = _iso_to_dt(header.get("lastModifiedAt"))
    # Fall back to the event stream when the header's stamps are absent/junk —
    # the events are the source of truth for when work actually happened.
    event_times = [t for t in (_iso_to_dt(e.get("timestamp")) for e in events) if t is not None]
    if session_start is None:
        session_start = event_times[0] if event_times else datetime.now(UTC)
    if session_end is None or session_end < session_start:
        session_end = event_times[-1] if event_times else session_start
    if session_end < session_start:
        session_end = session_start

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    # toolCallId -> (tool_call payload, owning turn's span_id, call time).
    pending_calls: dict[str, tuple[dict, str, datetime, dict | None]] = {}

    turn_num = 0
    turn: SpanData | None = None
    turn_output_parts: list[str] = []
    interrupts_total = 0
    interrupts_during_tool = 0
    total_credits = 0.0
    seen_unrecognised: set[tuple[str, str]] = set()

    def _close_turn() -> None:
        nonlocal turn, turn_output_parts
        if turn is not None:
            turn.attributes["output.value"] = "\n".join(turn_output_parts)[:_TRUNCATE]
        # Flush this turn's still-open tool calls as dangling spans (no result —
        # session/turn ended mid-call; the call's own status decides OK vs ERROR)
        # and clear the map. Clearing per-turn is what keeps the `turn_end`
        # during-tool-use interrupt check accurate: a dangling call from an
        # earlier turn must not make a later cancelled turn look mid-tool.
        for call_payload, tool_turn_id, call_ts, call_event in pending_calls.values():
            dangling = _build_tool_span(
                trace_id, tool_turn_id, call_payload, None, call_ts, call_ts
            )
            dangling.attributes.update(source_attrs(call_event))
            all_spans.append(dangling)
        pending_calls.clear()
        turn = None
        turn_output_parts = []

    def _open_turn(ts: datetime, input_value: str = "", src: dict | None = None) -> SpanData:
        nonlocal turn_num, turn
        turn_num += 1
        turn = SpanData(
            span_id=f"{trace_id}:turn:{turn_num}",
            trace_id=trace_id,
            parent_id=root_id,
            name=f"Turn {turn_num}",
            span_kind="CHAIN",
            status_code="OK",
            start_time=ts,
            end_time=ts,
            attributes={
                "input.value": input_value[:_TRUNCATE],
                "output.value": "",
                # Kiro records the workspace once on the header, not per event,
                # so every turn carries the same cwd - mirrors Codex/Cursor.
                "cwd": cwd,
                # Kiro bills credits and reports no token counts — these stay 0,
                # matching Cursor's no-native-usage default (see module docstring).
                "llm.token_count.prompt": 0,
                "llm.token_count.completion": 0,
                "llm.token_count.total_billed_prompt": 0,
                "llm.token_count.input_fresh": 0,
                "llm.token_count.cache_read": 0,
                "llm.token_count.cache_write": 0,
                # Kiro's transcript never names the model.
                "llm.model_name": "",
            },
        )
        turn.attributes.update(source_attrs(src))
        all_spans.append(turn)
        return turn

    prev_ts = session_start
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        ts = _iso_to_dt(event.get("timestamp")) or prev_ts
        prev_ts = ts

        if ptype == "user":
            _close_turn()
            content = payload.get("content")
            _open_turn(ts, content if isinstance(content, str) else "", event)
            continue

        if ptype == "turn_end":
            if payload.get("stopReason") == "cancelled":
                interrupts_total += 1
                # A cancel with tool calls still awaiting results is an
                # interrupt DURING tool use, counted separately.
                if pending_calls:
                    interrupts_during_tool += 1
            if turn is not None:
                turn.end_time = ts
            continue

        if ptype == "usage_summary":
            summaries = payload.get("promptTurnSummaries")
            if isinstance(summaries, list):
                for summary in summaries:
                    if isinstance(summary, dict):
                        usage = summary.get("usage")
                        # `bool` is an int subclass, so it passes a plain
                        # numeric check while `_as_float` deliberately returns
                        # 0.0 for it. Treat it as non-numeric here or a
                        # malformed `usage: true` is counted as zero silently.
                        if usage is not None and (
                            isinstance(usage, bool) or not isinstance(usage, int | float)
                        ):
                            # A non-numeric usage value is coerced to 0.0 by
                            # _as_float below, under-counting credits with
                            # nothing to show for it.
                            key = ("usage_summary", type(usage).__name__)
                            if key not in seen_unrecognised:
                                seen_unrecognised.add(key)
                                logger.debug(
                                    "session %s: usage_summary usage value has "
                                    "non-numeric type %s; counted as 0 credits",
                                    trace_id,
                                    type(usage).__name__,
                                )
                        total_credits += _as_float(usage)
            continue

        if ptype not in ("assistant", "tool_call", "tool_result"):
            # pending_interaction / session_metadata / session_event / sub_agent_*
            # are dropped on purpose and appear in ordinary sessions, so warning
            # on them means every session logs a warning nobody should act on.
            # A genuinely NEW vendor ptype is the one worth naming.
            if _is_intentionally_ignored_ptype(ptype):
                continue
            key = ("ptype", str(ptype))
            if key not in seen_unrecognised:
                seen_unrecognised.add(key)
                logger.debug(
                    "session %s: kiro event has unrecognised payload type %r; record dropped",
                    trace_id,
                    ptype,
                )
            continue

        if turn is None:
            # Agent-initiated activity before any user message still needs a
            # turn to live under (same fallback as cursor_store.py).
            _open_turn(ts, "", event)
        assert turn is not None  # for type checkers; _open_turn always sets it
        turn.end_time = ts

        if ptype == "assistant":
            content = payload.get("content")
            if isinstance(content, str) and content:
                turn_output_parts.append(content)
        elif ptype == "tool_call":
            call_id = payload.get("toolCallId")
            if isinstance(call_id, str) and call_id:
                pending_calls[call_id] = (payload, turn.span_id, ts, event)
        elif ptype == "tool_result":
            call_id = payload.get("toolCallId")
            call_payload, tool_turn_id, call_ts, call_event = pending_calls.pop(
                call_id if isinstance(call_id, str) else "",
                (None, turn.span_id, ts, None),
            )
            tool_span = _build_tool_span(trace_id, tool_turn_id, call_payload, payload, call_ts, ts)
            # anchored to the line that requested the call (the
            # result line when the call was never seen).
            tool_span.attributes.update(source_attrs(call_event or event))
            all_spans.append(tool_span)
    _close_turn()

    session_signals = {
        # Kiro has no slash commands or skills; the keys stay present so
        # readers see "none used", not "unknown".
        "slash_commands": [],
        "skills_used": [],
        "interrupts": {"total": interrupts_total, "during_tool_use": interrupts_during_tool},
    }

    root_span = SpanData(
        span_id=root_id,
        trace_id=trace_id,
        parent_id=None,
        name="Session",
        span_kind="CHAIN",
        status_code="OK",
        start_time=session_start,
        end_time=session_end,
        attributes={
            "user.id": user_id or "",
            # Kiro's transcript never names the model, so provider/cost
            # attribution in session.py finds "" and skips — deliberate.
            "llm.model_name": "",
            "session.id": trace_id,
            "project.name": project_name or "",
            "harness.agent_session_id": str(header.get("id") or ""),
            "harness.session_signals": json.dumps(session_signals),
            # Kiro's billing unit. Carried so a future credit->USD rate can use
            # it; no consumer reads this yet, and session.cost_usd is
            # deliberately NOT stamped (we don't know the dollar value).
            "session.kiro.total_credits": total_credits,
            "session.kiro.agent_mode": str(header.get("agentMode") or ""),
            "session.kiro.autopilot": bool(header.get("autopilot")),
            "session.kiro.spec_workflow": str(header.get("specWorkflow") or ""),
        },
    )
    all_spans.insert(0, root_span)

    trace = TraceData(
        trace_id=trace_id,
        start_time=session_start,
        end_time=session_end,
        spans=all_spans,
    )
    return trace, all_spans


def _build_tool_span(
    trace_id: str,
    turn_id: str,
    call: dict | None,
    result: dict | None,
    start: datetime,
    end: datetime,
) -> SpanData:
    """One tool_call/tool_result pair -> a tool span.

    Tool names are aliased to the canonical vocabulary via `_TOOL_ALIASES`
    (unknown names pass through). Args are normalized so the shared
    `file_paths.py` extractor — which only matches a `file_path` key — finds
    Kiro's touched file, same approach `cursor_store.py` takes for its
    `path`/`paths` keys.

    A call is an ERROR when Kiro marked the call `status: "failed"` or the
    result reported `success: false`.
    """
    raw_name = (call or {}).get("toolName")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    tool_name = _TOOL_ALIASES.get(raw_name, raw_name)

    args = (call or {}).get("args", {})
    if isinstance(args, dict):
        args = _normalize_file_path_arg(args)
        if raw_name in {"fs_write", "fsWrite"} and "content" not in args and "text" in args:
            args = {**args, "content": args["text"]}
    input_value = args if isinstance(args, str) else json.dumps(args)

    output_value = ""
    is_error = (call or {}).get("status") == "failed"
    if result is not None:
        raw_result = result.get("content", "")
        output_value = raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
        if result.get("success") is False:
            is_error = True

    call_id = (call or {}).get("toolCallId") or f"{turn_id}:{start.isoformat()}"
    return SpanData(
        span_id=f"{trace_id}:tool:{call_id}",
        trace_id=trace_id,
        parent_id=turn_id,
        name=tool_name,
        span_kind="CHAIN",
        status_code=(
            "ERROR"
            if is_error
            else "OK"
            if isinstance(result, dict) and result.get("success") is True
            else "UNSET"
        ),
        start_time=start,
        end_time=end,
        attributes={
            "tool.name": tool_name,
            "code.edit_evidence": extract_edit_evidence(
                tool_name,
                input_value,
                successful=isinstance(result, dict)
                and result.get("success") is True
                and not is_error,
            ),
            "input.value": input_value[:_TRUNCATE],
            "output.value": output_value[:_TRUNCATE],
        },
    )


def _normalize_file_path_arg(args: dict) -> dict:
    """Alias Kiro's `path`/`paths` arg keys to `file_path`.

    Kiro's read/edit/write tools
    (`read_file`, `fs_write`, `fs_append`, `str_replace`, `list_directory`) key
    the touched file as `"path"`; `read_files` keys it as a `"paths"` list.
    `file_paths.py`'s shared extractor only matches `"file_path"`, so the
    translation happens here rather than widening the shared module — the same
    call `cursor_store.py::_normalize_file_path_arg` makes.

    Purely additive: only fills `file_path` when absent, never mutates or
    removes the original key. `execute_bash`'s `command` is left untouched,
    matching how Claude's `Bash` calls get no `file_path`.

    Args:
        args: The tool call's raw `args` dict (already confirmed a dict).

    Returns:
        `args` unchanged, or a shallow copy with `file_path` added.
    """
    if "file_path" in args:
        return args
    path = args.get("path")
    if isinstance(path, str) and path:
        return {**args, "file_path": path}
    paths = args.get("paths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0]:
        return {**args, "file_path": paths[0]}
    return args


def _header_cwd(header: dict) -> str:
    """The session's workspace root — `workspacePaths[0]`, or "" when absent.

    Machine-independent (Kiro records the real workspace path the developer had
    open), so it survives copying and drives project_name + the repo resolver,
    same as every other agent's cwd.
    """
    paths = header.get("workspacePaths")
    if isinstance(paths, list) and paths:
        first = paths[0]
        if isinstance(first, str) and first:
            return first.rstrip("/")
    return ""


def _parse_ndjson(data: bytes) -> tuple[dict | None, list[dict]]:
    """Split the transcript into `(header, events)`.

    Line 1 must be the `kiro_session` header; every subsequent dict with a
    `payload` is an event. Malformed/absent header -> `(None, [])`.
    """
    lines = parse_jsonl_records(data)  # source anchors on every event
    if not lines or lines[0].get("type") != "kiro_session":
        return None, []

    header, rest = lines[0], lines[1:]
    events = [e for e in rest if isinstance(e.get("payload"), dict)]
    # The materializer writes these timestamp-sorted; don't trust it - turn assembly
    # depends on order, and a hand-edited or re-merged file could drift.
    events.sort(key=lambda e: str(e.get("timestamp") or ""))
    return header, events


def _iso_to_dt(value: object) -> datetime | None:
    """ISO-8601 string (e.g. `"2026-07-17T07:38:01.179Z"`) -> aware UTC datetime.

    Returns None when missing/unparsable. A naive result is assumed UTC,
    matching how Kiro writes them.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


# Kiro payload types this parser drops ON PURPOSE. Prefix matching because Kiro
# versions the sub_agent_* family independently, and a new member of a known
# inert family is not the vendor change this warning is looking for.
_IGNORED_PTYPE_PREFIXES = (
    "pending_interaction",
    "session_metadata",
    "session_event",
    "sub_agent_",
)

# Exact types, not families. Everything in the tuple above names a prefix Kiro
# really does expand (`sub_agent_started`, `session_event_*`), but a type that is
# a whole name on its own has to match exactly: as a prefix, `turn_start` would
# also swallow a future `turn_start_v2`, and silently dropping a vendor addition
# is the failure these warnings exist to catch.
_IGNORED_PTYPES = frozenset(
    {
        # The opening marker of every turn, carrying only an executionId;
        # `turn_end` above is the one that has something to say. It appears in
        # ordinary well-formed sessions, so warning about it means warning
        # once per session about ordinary traffic, and a warning people
        # always see is a warning people mute.
        "turn_start",
    }
)


def _is_intentionally_ignored_ptype(ptype: object) -> bool:
    """True when this payload type is a known inert family, not a new one."""
    if not isinstance(ptype, str):
        return False
    return ptype in _IGNORED_PTYPES or ptype.startswith(_IGNORED_PTYPE_PREFIXES)


def _as_float(value: object) -> float:
    """Coerce a credit-usage field to a non-negative float (0.0 on junk)."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, int | float):
        return max(0.0, float(value))
    return 0.0


__all__ = ["parse_kiro"]
