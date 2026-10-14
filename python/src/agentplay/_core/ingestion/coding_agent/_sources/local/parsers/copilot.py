"""Parse GitHub Copilot CLI ``events.jsonl`` sessions into the universal SpanData shape.

Copilot CLI (the ``copilot`` binary from the ``@github/copilot`` npm package)
writes one append-only event log per session:

    ~/.copilot/session-state/<session-id>/events.jsonl      (CLI >= 1.0.11)
    ~/.copilot/session-state/<session-id>.jsonl             (legacy flat layout)

Every line is an envelope ``{"type", "data", "id", "timestamp", "parentId"}``
with a dotted ``type``. The events this parser reads, verified against the
Copilot CLI 1.0.82 bundle and real local transcripts:

    session.start            sessionId, copilotVersion, selectedModel,
                             context{cwd, gitRoot, branch, repository, hostType}
    session.resume           context{cwd, ...} (a resumed session may move dirs)
    session.model_change     newModel, previousModel, reasoningEffort
    session.mode_changed     newMode (interactive | autopilot | plan ...)
    user.message             content; delivery="steering" is a mid-turn
                             injection; parentToolCallId / parentAgentTaskId
                             mark a sub-agent's prompt, not the developer's
    assistant.turn_end       closes the turn (turnId)
    assistant.message        content, model, outputTokens,
                             toolRequests[{toolCallId, name, arguments}],
                             parentToolCallId on sub-agent traffic
    tool.execution_start     toolCallId, toolName, arguments (request fallback)
    tool.execution_complete  toolCallId, success, result{content} | error{message},
                             model, toolTelemetry{metrics{linesAdded, linesRemoved},
                             properties{filePaths: JSON-encoded string array}}
    abort                    the developer cancelled the running turn
    session.truncation       Copilot dropped context (its compaction analogue)
    session.usage_checkpoint totalPremiumRequests (Copilot's billing unit)
    session.shutdown         modelMetrics{<model>: {requests{count}, usage{
                             inputTokens, outputTokens, cacheReadTokens,
                             cacheWriteTokens}}}: the only session-wide token source

# Tokens

Copilot never persists per-request input tokens. The ``assistant.usage`` event
that carries them is emitted ephemerally (streamed to attached clients and
filtered out of ``events.jsonl``). What survives on disk is ``outputTokens``
per ``assistant.message`` and the per-model ``session.shutdown.modelMetrics``
aggregate written when the session ends. So turn spans carry real completion
tokens and zero prompt / cache tokens, and when ``session.shutdown`` is present
the root span carries the billed session totals as ``session.token_count.*``,
which ``session_pure.aggregate_session_spans`` prefers over the turn sums
(the same override Cursor's billed-usage path uses). A session that has not
shut down yet is priced from completion tokens only until its next re-index.
``inputTokens`` is the TOTAL input count, inclusive of cache reads and writes.
This is unlike Anthropic's ``input_tokens`` field despite the similar name:
fresh input = max(input - cache_read - cache_write, 0). The prompt total must
therefore use ``inputTokens`` directly or cached input is counted twice.

# Turns, tools, sub-agents

``user.message`` opens a turn; ``assistant.turn_end`` closes it. A steering
message is appended to the open turn's input rather than starting a new one.
Sub-agent traffic (any event carrying ``parentToolCallId``) stays inside the
turn that spawned it, since its tool calls and tokens are that turn's spend,
and is counted on the root as ``harness.sidechain_count``, the Claude Code
``isSidechain`` analogue. Tool calls pair the request (``toolRequests[]`` or
``tool.execution_start``) with ``tool.execution_complete`` by ``toolCallId``;
a call that never completes becomes a zero-length UNSET span. Raw tool names are
aliased to the canonical vocabulary (``_TOOL_ALIASES``) so the read / edit /
shell counters, the blind-edit ordering and the sub-agent and skill capability
detectors see Copilot sessions the way they see Claude Code ones.

It reads far more than ``user.message`` / ``assistant.message`` /
``tool.execution_complete``: it also emits harness metadata, cwd, session
totals and transcript anchors.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
import json
import logging

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._paths import basename as path_basename
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_HASH_KEY,
    SOURCE_KIND_JSONL,
    SOURCE_OFFSET_KEY,
    parse_dt,
    parse_lines,
)
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    creation_output_attributes,
)
from agentplay._core.ingestion.coding_agent.skill_names import normalize_skill_name
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching every other local parser.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("copilot")

# Raw Copilot name of the skill-invocation tool (``arguments.skill`` names the
# skill). Compared lower-cased.
_SKILL_TOOL_RAW = "skill"

# Canonical names for which a tool-telemetry ``filePaths`` entry may stand in
# for a missing ``file_path`` argument. Deliberately excludes Bash: a shell
# command that happened to touch files must not start carrying a file_path,
# exactly as Claude's ``Bash`` never does (see ``file_paths.py``).
_FILE_TOOLS: frozenset[str] = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})

# Event types whose sub-agent variants count toward ``harness.sidechain_count``.
_SIDECHAIN_TYPES: frozenset[str] = frozenset(
    {"user.message", "assistant.message", "tool.execution_start", "tool.execution_complete"}
)


def parse_copilot(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Copilot CLI ``events.jsonl`` into a span tree.

    Args:
        data: raw JSONL bytes (one Copilot event envelope per line).
        trace_id: the source-prefixed session id (e.g. ``local:copilot:<uuid>``).
        user_id: identifier to stash on the root span's ``user.id``.
        project_name: project name resolved by ``LocalSource`` (from the
            ``session.start`` context cwd); derived from that cwd here when
            not given.

    Returns:
        ``(trace, all_spans)`` with the trace's ``spans`` matching the list.
        Empty when the file holds no parseable lines. A file with metadata
        but no conversation still yields a root span and no turns; the
        indexer (``LocalSource._index_copilot_file``) skips such files before
        they get here.
    """
    transcript_bytes = len(data)
    lines = parse_lines(data)
    if not lines:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []

    # Session metadata. Identity fields take the first value seen; state
    # fields (model, mode, cwd) take the last, since they change mid-session.
    agent_session_id = ""
    agent_version = ""
    entrypoint = ""
    repository = ""
    branch = ""
    cwd = ""
    model_name = ""
    effort_level = ""
    mode = ""
    sidechain_count = 0
    truncation_count = 0
    premium_requests: float | None = None
    shutdown_metrics: dict | None = None
    shutdown_model = ""
    interrupts_total = 0
    interrupts_during_tool = 0
    slash_counts: Counter[str] = Counter()
    skill_counts: Counter[str] = Counter()
    seen_ignored_etypes: set[str] = set()

    all_timestamps = [t for t in (_event_time(line) for line in lines) if t is not None]
    if all_timestamps:
        session_start = min(all_timestamps)
        session_end = max(all_timestamps)
    else:
        session_start = datetime.now(UTC)
        session_end = session_start

    # Turn assembly state.
    turn_num = 0
    turn: SpanData | None = None
    turn_output_parts: list[str] = []
    turn_completion = 0
    # The model in force for the OPEN turn: seeded from the session-level model
    # when the turn opens, then overwritten by the `model` each of its own
    # assistant.message / tool.execution_complete lines carries. Kept apart
    # from `model_name` because a `session.model_change` between two turns
    # lands BEFORE the next user.message closes the previous turn, and stamping
    # the session-level value at close would credit the earlier turn's work to
    # the later model.
    turn_model = ""
    # toolCallId -> request record (see _new_tool_record); popped on completion.
    pending_tools: dict[str, dict] = {}

    def _close_turn() -> None:
        nonlocal turn, turn_output_parts, turn_completion
        if turn is None:
            return
        turn.attributes["output.value"] = "\n".join(turn_output_parts)[:_TRUNCATE]
        turn.attributes["llm.token_count.completion"] = turn_completion
        # The model a turn ran on is only known once its assistant messages
        # have been read, so it is stamped at close, not at open.
        turn.attributes["llm.model_name"] = turn_model
        # Requests that never completed (session or turn ended mid-call) become
        # zero-length spans; flushing per turn keeps the abort during-tool-use
        # check honest for later turns.
        for rec in pending_tools.values():
            all_spans.append(_build_tool_span(trace_id, rec, None, rec["start"]))
        pending_tools.clear()
        turn = None
        turn_output_parts = []
        turn_completion = 0

    def _open_turn(
        ts: datetime, input_value: str, offset: object, source_hash: object = None
    ) -> SpanData:
        nonlocal turn_num, turn, turn_model
        turn_num += 1
        turn_model = model_name
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
                # The cwd in force when the turn opened (session.start, updated
                # by session.resume). Used to resolve the turn's repo, as for every agent.
                "cwd": cwd,
                # Per-request input tokens are not on disk (see module docstring):
                # prompt / cache attrs stay 0 and the session total rides on the
                # root span instead.
                "llm.token_count.prompt": 0,
                "llm.token_count.completion": 0,
                "llm.token_count.total_billed_prompt": 0,
                "llm.token_count.input_fresh": 0,
                "llm.token_count.cache_read": 0,
                "llm.token_count.cache_write": 0,
                "llm.model_name": model_name,
            },
        )
        # transcript-anchored turn identity, the byte offset of the
        # line that opened the turn.
        if offset is not None:
            turn.attributes["source.byte_offset"] = offset
        # the universal anchor block.
        turn.attributes["source.kind"] = SOURCE_KIND_JSONL
        if source_hash is not None:
            turn.attributes["source.content_hash"] = source_hash
        all_spans.append(turn)
        return turn

    def _record_request(
        call_id: str, name: object, args: object, ts: datetime, offset: object
    ) -> None:
        """Remember a tool request so its completion can be paired later."""
        assert turn is not None  # callers open a turn first
        rec = pending_tools.get(call_id)
        if rec is None:
            rec = _new_tool_record(call_id, name, args, ts, offset, turn.span_id, source_hash)
            pending_tools[call_id] = rec
            _note_skill(rec, skill_counts)
            return
        # A request seen twice (toolRequests then execution_start): fill gaps
        # only, never overwrite the first sighting.
        if not rec["name"] and isinstance(name, str) and name:
            rec["name"] = name
            _note_skill(rec, skill_counts)
        if rec["args"] is None and args is not None:
            rec["args"] = args

    prev_ts = session_start
    for line in lines:
        etype = line.get("type")
        if not isinstance(etype, str):
            continue
        payload = line.get("data")
        payload = payload if isinstance(payload, dict) else {}
        ts = _event_time(line) or prev_ts
        prev_ts = ts
        offset = line.get(SOURCE_OFFSET_KEY)
        source_hash = line.get(SOURCE_HASH_KEY)

        parent_call = payload.get("parentToolCallId") or payload.get("parentAgentTaskId")
        is_sidechain = isinstance(parent_call, str) and bool(parent_call)
        if is_sidechain and etype in _SIDECHAIN_TYPES:
            sidechain_count += 1

        if etype == "session.start":
            sid = payload.get("sessionId")
            if not agent_session_id and isinstance(sid, str):
                agent_session_id = sid
            ver = payload.get("copilotVersion")
            if not agent_version and isinstance(ver, str):
                agent_version = ver
            selected = model_id_or_none(payload.get("selectedModel"))
            if selected:
                model_name = selected
            ctx = payload.get("context")
            if isinstance(ctx, dict):
                cwd = _str_field(ctx, "cwd") or cwd
                branch = _str_field(ctx, "branch") or branch
                repository = _str_field(ctx, "repository") or repository
                entrypoint = entrypoint or _str_field(ctx, "hostType")
            continue

        if etype == "session.resume":
            ctx = payload.get("context")
            if isinstance(ctx, dict):
                cwd = _str_field(ctx, "cwd") or cwd
                branch = _str_field(ctx, "branch") or branch
                repository = _str_field(ctx, "repository") or repository
            selected = model_id_or_none(payload.get("selectedModel"))
            if selected:
                model_name = selected
            continue

        if etype == "session.model_change":
            new_model = model_id_or_none(payload.get("newModel"))
            if new_model:
                model_name = new_model
            effort = payload.get("reasoningEffort")
            if isinstance(effort, str) and effort:
                effort_level = effort
            continue

        if etype == "session.mode_changed":
            new_mode = payload.get("newMode")
            if isinstance(new_mode, str) and new_mode:
                mode = new_mode
            continue

        if etype == "session.info":
            # Legacy builds carried no session.start context; the folder-trust
            # notice ("Folder /x/y has been added to trusted folders.") is the
            # only cwd hint they wrote.
            if not cwd and payload.get("infoType") == "folder_trust":
                cwd = _folder_trust_path(payload.get("message")) or cwd
            continue

        if etype == "session.truncation":
            truncation_count += 1
            continue

        if etype == "session.usage_checkpoint":
            requests = _as_float(payload.get("totalPremiumRequests"))
            if requests is not None:
                premium_requests = requests
            continue

        if etype == "session.shutdown":
            metrics = payload.get("modelMetrics")
            if isinstance(metrics, dict) and metrics:
                shutdown_metrics = metrics
            current = model_id_or_none(payload.get("currentModel"))
            if current:
                shutdown_model = current
            requests = _as_float(payload.get("totalPremiumRequests"))
            if requests is not None:
                premium_requests = requests
            continue

        if etype == "abort":
            interrupts_total += 1
            # A cancel with tool calls still awaiting results is an interrupt
            # DURING tool use, the distinction the Claude Code signals draw.
            if pending_tools:
                interrupts_during_tool += 1
            if turn is not None:
                turn.end_time = ts
            continue

        if etype == "user.message":
            if is_sidechain:
                continue  # a sub-agent's prompt is not the developer's
            content = payload.get("content")
            text = content if isinstance(content, str) else ""
            stripped = text.lstrip()
            if stripped.startswith("/"):
                command = stripped.split(None, 1)[0].lstrip("/")
                if command:
                    slash_counts[command] += 1
            if payload.get("delivery") == "steering" and turn is not None:
                # Steering is delivered INTO the running turn, so it extends the
                # turn's input rather than opening a new one.
                joined = f"{turn.attributes['input.value']}\n{text}".strip()
                turn.attributes["input.value"] = joined[:_TRUNCATE]
                turn.end_time = ts
                continue
            _close_turn()
            _open_turn(ts, text, offset, source_hash)
            continue

        if etype == "assistant.turn_end":
            if turn is not None:
                turn.end_time = ts
            continue

        if etype not in ("assistant.message", "tool.execution_start", "tool.execution_complete"):
            # Reasoning, hooks, subagent lifecycle and system notices are dropped
            # on purpose. A genuinely NEW vendor etype would drop just as
            # silently, which is the failure this warning exists to catch, so
            # only an etype outside the known-intentional set is worth a warning:
            # warning on every skip would add many lines per session of ordinary
            # traffic, and a warning people always see is a warning people mute. Once per distinct value per session either way.
            if etype not in seen_ignored_etypes:
                seen_ignored_etypes.add(etype)
                if _is_intentionally_ignored(etype):
                    logger.debug(
                        "session %s: copilot event type %r skipped by design",
                        trace_id,
                        etype,
                    )
                else:
                    logger.debug(
                        "session %s: copilot event type %r is not handled; record "
                        "dropped. If Copilot added this type, the parser needs it.",
                        trace_id,
                        etype,
                    )
            continue

        if turn is None:
            # Agent-initiated activity before any user message still needs a
            # turn to live under (same fallback as cursor_store / kiro).
            _open_turn(ts, "", offset, source_hash)
        assert turn is not None  # for type checkers; _open_turn always sets it
        turn.end_time = ts

        line_model = model_id_or_none(payload.get("model"))
        if line_model:
            model_name = line_model
            turn_model = line_model

        if etype == "assistant.message":
            out_tokens = payload.get("outputTokens")
            if isinstance(out_tokens, int | float) and not isinstance(out_tokens, bool):
                turn_completion += max(0, int(out_tokens))
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                turn_output_parts.append(content)
            requests_list = payload.get("toolRequests")
            if isinstance(requests_list, list):
                for req in requests_list:
                    if not isinstance(req, dict):
                        continue
                    call_id = req.get("toolCallId")
                    if isinstance(call_id, str) and call_id:
                        _record_request(call_id, req.get("name"), req.get("arguments"), ts, offset)
            continue

        call_id = payload.get("toolCallId")
        if not (isinstance(call_id, str) and call_id):
            continue

        if etype == "tool.execution_start":
            _record_request(call_id, payload.get("toolName"), payload.get("arguments"), ts, offset)
            continue

        # tool.execution_complete
        rec = pending_tools.pop(call_id, None)
        if rec is None:
            # A result whose request line was lost (truncated head, hand-edited
            # file): still a tool call that ran, just without its arguments.
            rec = _new_tool_record(call_id, None, None, ts, offset, turn.span_id, source_hash)
        all_spans.append(_build_tool_span(trace_id, rec, payload, ts))

    # Closes the final turn and flushes any tool calls still open at session
    # end (dangling spans), see _close_turn.
    _close_turn()

    if not project_name and cwd:
        project_name = path_basename(cwd) or None

    session_signals = {
        "slash_commands": [{"name": n, "count": c} for n, c in slash_counts.most_common()],
        # ``path`` carries the canonical skill name, the same contract the
        # Claude Code signals use (see _jsonl_utils.extract_session_signals).
        "skills_used": [{"path": p, "count": c} for p, c in skill_counts.most_common()],
        # Copilot never injects a SKILL.md body into the transcript.
        "skill_definitions": [],
        "interrupts": {"total": interrupts_total, "during_tool_use": interrupts_during_tool},
    }

    root_attrs: dict = {
        "user.id": user_id or "",
        "llm.model_name": model_name or shutdown_model,
        "session.id": trace_id,
        "project.name": project_name or "",
        "harness.agent_session_id": agent_session_id,
        "harness.agent_version": agent_version,
        "harness.effort_level": effort_level,
        # Copilot's session mode (interactive | autopilot | plan) is the closest
        # analogue to Claude Code's permission mode: it decides how much the
        # agent may do without asking.
        "harness.permission_mode": mode,
        # The surface the session ran on: "cli", "github" (cloud), ...
        "harness.entrypoint": entrypoint,
        "harness.sidechain_count": sidechain_count,
        "harness.session_signals": json.dumps(session_signals),
        # raw transcript length as read.
        "source.transcript_bytes": transcript_bytes,
        # Copilot-native session facts. Carried so repo attribution and a
        # premium-request -> USD conversion can use them without a re-parse.
        "session.copilot.repository": repository,
        "session.copilot.branch": branch,
        "session.copilot.truncation_count": truncation_count,
    }
    if premium_requests is not None:
        root_attrs["session.copilot.premium_requests"] = premium_requests

    totals = _shutdown_totals(shutdown_metrics, trace_id)
    if totals is not None:
        # Session-wide billed totals: consumers should prefer these over the
        # (prompt-less) turn sums when ``session.token_count.prompt`` is
        # present. ``session.cost_usd`` is deliberately NOT stamped: Copilot
        # bills premium requests, and the dollar value is the pricing layer's
        # call, not the parser's.
        root_attrs["session.token_count.prompt"] = totals["prompt"]
        root_attrs["session.token_count.completion"] = totals["completion"]
        root_attrs["session.token_count.cache_read"] = totals["cache_read"]
        root_attrs["session.token_count.cache_write"] = totals["cache_write"]
        root_attrs["session.copilot.request_count"] = totals["requests"]
        root_attrs["session.copilot.model_metrics"] = json.dumps(
            totals["per_model"], sort_keys=True
        )

    root_span = SpanData(
        span_id=root_id,
        trace_id=trace_id,
        parent_id=None,
        name="Session",
        span_kind="CHAIN",
        status_code="OK",
        start_time=session_start,
        end_time=session_end,
        attributes=root_attrs,
    )
    all_spans.insert(0, root_span)

    trace = TraceData(
        trace_id=trace_id,
        start_time=session_start,
        end_time=session_end,
        spans=all_spans,
    )
    return trace, all_spans


def _new_tool_record(
    call_id: str,
    name: object,
    args: object,
    ts: datetime,
    offset: object,
    turn_id: str,
    source_hash: object = None,
) -> dict:
    """The pending-request record a completion is later paired with."""
    return {
        "call_id": call_id,
        "name": name if isinstance(name, str) and name else "",
        "args": args,
        "start": ts,
        "offset": offset,
        "source_hash": source_hash,
        "turn_id": turn_id,
    }


def _note_skill(rec: dict, skill_counts: Counter[str]) -> None:
    """Count a ``skill`` tool request under its canonical skill name."""
    if rec["name"].lower() != _SKILL_TOOL_RAW:
        return
    args = _coerce_args(rec["args"])
    if not isinstance(args, dict):
        return
    raw = args.get("skill") or args.get("name")
    if isinstance(raw, str) and raw.strip():
        canonical = normalize_skill_name(raw.strip())
        if canonical is not None:
            skill_counts[canonical] += 1


def _build_tool_span(
    trace_id: str,
    rec: dict,
    result: dict | None,
    end: datetime,
) -> SpanData:
    """One request / completion pair -> a tool span.

    Tool names are aliased to the canonical vocabulary via ``_TOOL_ALIASES``
    (unknown names pass through). Args are normalized so the shared
    ``file_paths.py`` extractor, which only matches a ``file_path`` key, finds
    the touched file: Copilot's ``view`` / ``edit`` / ``create`` key it as
    ``path``. A call is an ERROR when the completion reported
    ``success: false``; a request with no completion (``result`` is None) is a
    zero-length UNSET span.
    """
    raw_name = rec["name"] or "unknown"
    if not isinstance(raw_name, str):
        raw_name = "unknown"
    tool_name = _TOOL_ALIASES.get(raw_name, raw_name)

    telemetry = result.get("toolTelemetry") if isinstance(result, dict) else None
    telemetry = telemetry if isinstance(telemetry, dict) else {}
    telemetry_paths = _telemetry_file_paths(telemetry) if tool_name in _FILE_TOOLS else []

    args = _coerce_args(rec["args"])
    description = ""
    if isinstance(args, dict):
        args = _normalize_file_path_arg(args, telemetry_paths)
        desc = args.get("description")
        description = desc if isinstance(desc, str) else ""
        input_value = json.dumps(args)
    elif args is None:
        input_value = ""
    else:
        input_value = str(args)

    output_value = ""
    is_error = False
    if result is not None:
        if result.get("success") is False:
            is_error = True
        body = result.get("result")
        if isinstance(body, dict):
            content = body.get("content")
            if isinstance(content, str):
                output_value = content
            elif content is not None:
                output_value = json.dumps(content)
        elif isinstance(body, str):
            output_value = body
        error = result.get("error")
        if not output_value and isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                output_value = message

    successful = isinstance(result, dict) and result.get("success") is True and not is_error
    command_attrs = creation_command_attributes(input_value, tool_name)
    creation_result = (
        (result.get("result") if command_attrs.get("pr.create.repository") else output_value)
        if successful
        else ""
    )
    attrs: dict = {
        "tool.name": tool_name,
        "code.edit_evidence": extract_edit_evidence(
            raw_name if raw_name == "apply_patch" else tool_name,
            input_value,
            successful=successful,
        ),
        "tool.description": description,
        "input.value": input_value[:_TRUNCATE],
        **command_attrs,
        **creation_output_attributes(
            creation_result,
            command_attrs.get("pr.create.provider"),
            command_attrs.get("pr.create.repository"),
        ),
        "output.value": output_value[:_TRUNCATE],
        # content-anchored identity. ``toolCallId`` is Copilot's own
        # id for the call, stable across re-parses by construction.
        "source.tool_use_id": rec["call_id"],
    }
    if rec["offset"] is not None:
        attrs["source.byte_offset"] = rec["offset"]
    attrs["source.kind"] = SOURCE_KIND_JSONL
    if rec.get("source_hash") is not None:
        attrs["source.content_hash"] = rec["source_hash"]
    metrics = telemetry.get("metrics")
    if isinstance(metrics, dict):
        for key, attr in (
            ("linesAdded", "tool.lines_added"),
            ("linesRemoved", "tool.lines_removed"),
        ):
            value = metrics.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                attrs[attr] = int(value)

    start = rec["start"]
    return SpanData(
        span_id=f"{trace_id}:tool:{rec['call_id']}",
        trace_id=trace_id,
        parent_id=rec["turn_id"],
        name=tool_name,
        span_kind="CHAIN",
        status_code="ERROR" if is_error else "OK" if successful else "UNSET",
        start_time=start,
        end_time=end if end >= start else start,
        attributes=attrs,
    )


def _coerce_args(args: object) -> object:
    """Tool arguments as recorded, decoding a JSON-encoded string form.

    Copilot writes ``arguments`` as an object; relayed sessions have been seen
    carrying the same payload as a JSON string. Anything else is returned as-is
    so the caller can still stringify it into ``input.value``.
    """
    if isinstance(args, str):
        stripped = args.strip()
        if stripped.startswith("{"):
            try:
                decoded = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return args
            return decoded if isinstance(decoded, dict) else args
    return args


def _normalize_file_path_arg(args: dict, telemetry_paths: list[str]) -> dict:
    """Alias Copilot's ``path`` arg key to ``file_path``.

    Copilot's file tools (``view``, ``edit``, ``create``, ``str_replace_editor``)
    key the touched file as ``path``; ``file_paths.py``'s shared extractor only
    matches ``file_path``, so the translation happens here, as the Cursor and
    Kiro parsers do for their own key names. When the args carry no path at
    all, the first ``toolTelemetry.properties.filePaths`` entry stands in (the
    caller only passes those for file-oriented tools).

    Purely additive: only fills ``file_path`` when absent, never mutates or
    removes the original key. The synthesized key is PREPENDED: ``input.value``
    is truncated to 2,000 characters and an ``edit`` carrying a large
    ``new_str`` would otherwise push ``file_path`` past the cut, and the file
    would silently vanish from blind-edit / files-touched attribution.
    """
    if "file_path" in args:
        return args
    for key in ("path", "filePath"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return {"file_path": value, **args}
    if telemetry_paths:
        return {"file_path": telemetry_paths[0], **args}
    return args


def _telemetry_file_paths(telemetry: dict) -> list[str]:
    """Decode ``toolTelemetry.properties.filePaths``, a JSON-encoded string array."""
    props = telemetry.get("properties")
    if not isinstance(props, dict):
        return []
    raw = props.get("filePaths")
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, str) and p]
    if not isinstance(raw, str) or not raw:
        return []
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return [p for p in decoded if isinstance(p, str) and p]


# Copilot event types this parser drops ON PURPOSE: session.*, model.*, hook.*, permission.*,
# subagent.*, skill.invoked, assistant.turn_start. Prefixes rather than an exact
# list because Copilot versions these families independently, and a new member
# of a known-inert family is not the event we are trying to catch.
_IGNORED_ETYPE_PREFIXES = (
    "session.",
    "model.",
    "hook.",
    "permission.",
    "subagent.",
    "skill.",
    "assistant.turn_",
    # `system.message` is the CLI's own system prompt and `system.notification`
    # is a shell/subagent completion notice. Both carry only role/content/
    # kind, no tokens and no tool calls. They are the "system notices dropped on
    # purpose" the skip branch's comment names, so they belong here rather than
    # producing warnings about records nothing wants.
    "system.",
)


def _is_intentionally_ignored(etype: str) -> bool:
    """True when this event type is a known-inert family, not a new vendor type."""
    return etype.startswith(_IGNORED_ETYPE_PREFIXES)


def _shutdown_totals(metrics: dict | None, trace_id: str = "<unknown>") -> dict | None:
    """Sum ``session.shutdown.modelMetrics`` across models.

    Returns ``{prompt, completion, cache_read, cache_write, requests,
    per_model}`` or None when there are no metrics or every count is zero
    (a zero aggregate must not override real per-turn completion tokens).
    Copilot's ``inputTokens`` is already the total input count, including cache
    reads and writes. ``input`` in the normalized per-model detail remains the
    fresh component that pricing code expects.
    """
    if not metrics:
        return None
    prompt = completion = cache_read = cache_write = requests = 0
    per_model: dict[str, dict[str, int]] = {}
    for model, entry in metrics.items():
        if not isinstance(model, str) or not isinstance(entry, dict):
            # A malformed modelMetrics key/entry drops that model's whole
            # token total silently otherwise; each bad key is unique per
            # dict, so this can't flood the log.
            logger.debug(
                "session %s: copilot shutdown.modelMetrics entry %r is malformed "
                "(key or value has the wrong shape); its token total is dropped",
                trace_id,
                model,
            )
            continue
        usage = entry.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        req = entry.get("requests")
        req = req if isinstance(req, dict) else {}
        total_input = _as_int(usage.get("inputTokens"))
        out = _as_int(usage.get("outputTokens"))
        read = _as_int(usage.get("cacheReadTokens"))
        write = _as_int(usage.get("cacheWriteTokens"))
        fresh = max(total_input - read - write, 0)
        count = _as_int(req.get("count"))
        per_model[model] = {
            "input": fresh,
            "output": out,
            "cache_read": read,
            "cache_write": write,
            "requests": count,
        }
        prompt += total_input
        completion += out
        cache_read += read
        cache_write += write
        requests += count
    if prompt == 0 and completion == 0:
        return None
    return {
        "prompt": prompt,
        "completion": completion,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "requests": requests,
        "per_model": per_model,
    }


def _folder_trust_path(message: object) -> str:
    """cwd from a folder-trust notice: "Folder <path> has been added to trusted folders."."""
    if not isinstance(message, str):
        return ""
    marker = "Folder "
    start = message.find(marker)
    if start == -1:
        return ""
    rest = message[start + len(marker) :]
    end = rest.find(" has been")
    path = rest if end == -1 else rest[:end]
    return path.strip()


def _event_time(line: dict) -> datetime | None:
    """The envelope's ISO-8601 ``timestamp`` as an aware datetime, None when junk."""
    raw = line.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = parse_dt(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _str_field(obj: dict, key: str) -> str:
    value = obj.get(key)
    return value.rstrip("/") if isinstance(value, str) and value else ""


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    return 0


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return max(0.0, float(value))
    return None


__all__ = ["parse_copilot"]
