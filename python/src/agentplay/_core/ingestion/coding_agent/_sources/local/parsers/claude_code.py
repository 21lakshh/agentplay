"""Parse a local Claude Code JSONL session into the universal SpanData shape.

Reads files written by Claude Code under `~/.claude/projects/<slug>/`.
Most of the work runs through the shared `_jsonl_utils` helpers. This parser additionally
de-duplicates turns that get replayed by session-continuation events
(Claude Code re-serialises earlier exchanges into a fresh session file
when you `/compact`, producing duplicate Turn timestamps).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from typing import Any

from agentplay._core.claude_edit_evidence import call_edit_evidence, result_patches
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    SOURCE_HASH_KEY,
    SOURCE_OFFSET_KEY,
    drop_untyped,
    extract_session_signals,
    extract_user_content,
    fold_subagent_skills,
    group_into_turns,
    parse_dt,
    parse_lines,
    safe_parse_dt,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.claude_cost_summary import (
    extract_cost_summary,
)
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    creation_output_attributes,
)
from agentplay._core.ingestion.coding_agent.skill_names import skill_call_attributes
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData, model_id_or_none

logger = logging.getLogger(__name__)


def _turn_index_for(at: datetime | None, turn_starts: list[datetime | None]) -> int:
    """1-based turn whose start is the latest at or before ``at``.

    A call before the first turn, or one whose turn has no clock, lands on
    turn 1; a call with no timestamp of its own lands on the last turn
    (subagent files are appended after the parent, so "latest" is the
    honest default). 0 when there are no turns at all.
    """
    if not turn_starts:
        return 0
    if at is None:
        return len(turn_starts)
    index = 1
    latest_start = None
    for i, started in enumerate(turn_starts, start=1):
        if (
            started is not None
            and started <= at
            and (latest_start is None or started > latest_start)
        ):
            index = i
            latest_start = started
    return index


def _merge_replayed_turns(turns: list[dict]) -> list[dict]:
    """Keep each original turn anchor and merge its replayed observations."""
    unique: dict[tuple[str, str], dict] = {}
    for turn in turns:
        user = turn.get("user")
        anchor = user if user is not None else turn["assistants"][0]
        key = (
            "user" if user is not None else "assistant",
            anchor.get("uuid") or anchor[SOURCE_HASH_KEY],
        )
        if key not in unique:
            unique[key] = turn
            continue
        original = unique[key]
        seen = {line[SOURCE_HASH_KEY] for line in original["assistants"]}
        for line in turn["assistants"]:
            if line[SOURCE_HASH_KEY] not in seen:
                original["assistants"].append(line)
                seen.add(line[SOURCE_HASH_KEY])
        for tool_id, result in turn["tool_results"].items():
            timestamp = turn["tool_result_timestamps"].get(tool_id)
            old_timestamp = original["tool_result_timestamps"].get(tool_id)
            if tool_id not in original["tool_results"] or (
                timestamp and (not old_timestamp or parse_dt(timestamp) >= parse_dt(old_timestamp))
            ):
                original["tool_results"][tool_id] = result
                original["tool_result_timestamps"][tool_id] = timestamp
    return list(unique.values())


def _message_key(line: dict) -> str:
    """Native message identity, with stable record identity for legacy records."""
    return line.get("message", {}).get("id") or line.get("uuid") or line[SOURCE_HASH_KEY]


def _fallback_iterations(line: dict) -> list | None:
    """Validate iteration evidence before choosing its accounting contract.

    Evidence that cannot be trusted is ignored (None), so the response keeps its
    top-level usage rather than aborting the session.
    """
    message = line.get("message", {})
    iterations = (message.get("usage") or {}).get("iterations")
    if iterations is None or iterations == []:
        return None
    if not isinstance(iterations, list) or not all(
        isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"]
        for item in iterations
    ):
        logger.debug("ignoring malformed Claude usage iterations")
        return None
    for item in iterations:
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            value = (
                item.get(field)
                if field in {"input_tokens", "output_tokens"}
                else item.get(field, 0)
            )
            if type(value) is not int or value < 0:
                logger.debug("ignoring Claude usage iterations with a bad token count")
                return None
    if len(iterations) == 1 and iterations[0].get("model") in (None, message.get("model")):
        # Native ordinary responses carry one message iteration with model=null.
        # They retain the top-level streaming usage contract.
        return None
    # Any other shape means the response served more than one billed attempt —
    # a fallback after a refusal, or a side call such as an advisor iteration
    # whose tokens the top-level usage does not include. Per-attempt accounting
    # is the only contract that bills all of them. An iteration without a model
    # was served by the response's own model, so only a present one is checked.
    if not all(
        model_id_or_none(item["model"]) for item in iterations if item.get("model") is not None
    ):
        logger.debug("ignoring Claude usage iterations with a bad model")
        return None
    return iterations


def _expand_fallback_requests(requests: dict) -> dict:
    """Use per-attempt usage when fallback makes top-level usage serving-only.

    Anthropic's refusals-and-fallback billing contract bills each attempt that
    produced output. An earlier refusal with no output is not billed. Retain
    the original response's source/clock, and distinguish attempts by position.
    Preliminary streaming fragments must not also become a top-level charge.
    """
    expanded = {}
    for key, (turn_num, fragments) in requests.items():
        snapshots = [line for line in fragments if _fallback_iterations(line) is not None]
        if not snapshots:
            expanded[key] = (turn_num, fragments)
            continue
        for line in snapshots:
            message = line["message"]
            usage = message["usage"]
            iterations = usage["iterations"]
            for index, item in enumerate(iterations):
                refused = index < len(iterations) - 1 or message.get("stop_reason") == "refusal"
                if refused and item["output_tokens"] == 0:
                    continue
                attempt_key = f"{key}:iteration:{index}"
                attempt_usage = {**item}
                # Fallback messages never carry `server_tool_use` per iteration --
                # only the message-level `usage` has it. Same carry-forward as
                # speed/service_tier below, or a fallback response's web
                # search/fetch counts become 0 on the billed attempt.
                for field in ("speed", "service_tier", "server_tool_use"):
                    if (
                        field not in attempt_usage
                        and field in usage
                        and index == len(iterations) - 1
                    ):
                        attempt_usage[field] = usage[field]
                # Fallback configuration can override speed per attempt. The
                # serving model's mode cannot establish a prior attempt's price.
                if index < len(iterations) - 1 and "speed" not in attempt_usage:
                    attempt_usage["speed"] = "unknown"
                attempt = {
                    **line,
                    "requestId": f"{line['requestId']}:iteration:{index}"
                    if line.get("requestId")
                    else None,
                    "message": {
                        **message,
                        "id": attempt_key,
                        "model": item.get("model") or message.get("model"),
                        "usage": attempt_usage,
                    },
                }
                expanded.setdefault(attempt_key, (turn_num, []))[1].append(attempt)
    return expanded


def _fallback_tool_request_id(line: dict, request_id: str | None) -> str | None:
    """Point tools in a fallback response at the billed attempt they came from."""
    if not request_id:
        return None
    if ":iteration:" in request_id:
        return request_id
    iterations = _fallback_iterations(line)
    if not iterations:
        return request_id
    billed_indexes = []
    for index, item in enumerate(iterations):
        refused = (
            index < len(iterations) - 1 or line.get("message", {}).get("stop_reason") == "refusal"
        )
        if not (refused and item["output_tokens"] == 0):
            billed_indexes.append(index)
    if not billed_indexes:
        return request_id
    return f"{request_id}:iteration:{billed_indexes[-1]}"


def _tool_request_id(line: dict, msg_meta: dict, fallback: str | None) -> str | None:
    """Resolve a tool to the highest billed fallback attempt for its message."""
    prefix = f"{_message_key(line)}:iteration:"
    candidates = [entry for entry in msg_meta.items() if entry[0].startswith(prefix)]
    if candidates:
        _key, metadata = max(
            candidates,
            key=lambda entry: int(entry[0].rsplit(":iteration:", 1)[1]),
        )
        return metadata.get("request_id") or fallback
    return fallback


def _subagent_edit_calls(
    trace_id: str,
    turns: list[dict],
    main_lines: list[dict],
    sidechain_lines: list[dict],
    native_results: dict[str, dict | None],
) -> list[dict[str, Any]]:
    """Captured code edits made by Agent-tool subagents, in completion order.

    Deliberately NOT tool spans: every span-derived metric (tool counts, error
    rate, tool distribution) selects spans by ``tool.name`` and
    stays parent-only. These entries exist for code-authorship replay alone and
    are kept in a separate ``subagent_edit_calls`` array. Each keeps its
    own completion time, so it replays in its real order among the parent's
    edits even when the subagent ran in the background long after its spawning
    call returned.

    Args:
        trace_id: The session's trace id; turn ids derive from it.
        turns: The parent's turns, whose start times place each call.
        main_lines: Parent lines. A tool_use id recorded there with its
            successful result is already a parent tool span (Claude Code echoes
            some subagent lines into the parent), so it is skipped here rather
            than counted twice.
        sidechain_lines: Subagent lines (``isSidechain: true``).
        native_results: ``result_patches`` over the whole folded transcript.

    Returns:
        One dict per successful call with captured, non-empty evidence:
        ``turn_id``, ``tool_name``, ``subagent_id``, ``native_call_id``,
        ``started_at``/``completed_at`` (ISO, UTC), ``cwd``, ``input_value``
        (the same 2000-char input a tool span carries, for the input digest)
        and ``edit_evidence``. Ordered by completion time, then call id.
    """

    def blocks(line: dict, kind: str) -> list[dict]:
        message = line.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        return [b for b in content if isinstance(b, dict) and b.get("type") == kind]

    # An echoed call is the parent's only when the parent also recorded its
    # successful result; otherwise its span is unsuccessful and only the
    # sidechain carries the edit.
    parent_used = {b.get("id") for line in main_lines for b in blocks(line, "tool_use")}
    parent_calls = {
        b.get("tool_use_id")
        for line in main_lines
        for b in blocks(line, "tool_result")
        if b.get("tool_use_id") in parent_used and not b.get("is_error")
    }
    results: dict[str, tuple[dict, Any]] = {}
    for line in sidechain_lines:
        for block in blocks(line, "tool_result"):
            results[block.get("tool_use_id")] = (block, line.get("timestamp"))
    starts = [safe_parse_dt((turn.get("user") or {}).get("timestamp")) for turn in turns]
    calls: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for line in sidechain_lines:
        for block in blocks(line, "tool_use"):
            call_id = block.get("id")
            if not isinstance(call_id, str) or not call_id:
                continue
            if call_id in parent_calls or call_id in calls or call_id not in results:
                continue
            result, result_at = results[call_id]
            completed = safe_parse_dt(result_at)
            if result.get("is_error") or completed is None or not line.get("agentId"):
                continue
            tool_name, tool_input = block.get("name"), block.get("input", {})
            evidence = call_edit_evidence(
                tool_name,
                tool_input,
                native_results.get(call_id),
                has_native=call_id in native_results,
                successful=True,
            )
            started = safe_parse_dt(line.get("timestamp"))
            turn_num = _turn_index_for(started or completed, starts)
            if evidence["status"] != "captured" or not evidence["edits"] or not turn_num:
                continue
            input_str = json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
            calls[call_id] = (
                completed,
                {
                    "turn_id": f"{trace_id}:turn:{turn_num}",
                    "tool_name": tool_name,
                    "subagent_id": line["agentId"],
                    "native_call_id": call_id,
                    "started_at": started.astimezone(UTC).isoformat() if started else None,
                    "completed_at": completed.astimezone(UTC).isoformat(),
                    "cwd": line.get("cwd") or "",
                    "input_value": input_str[:2000],
                    "edit_evidence": evidence,
                },
            )
    ordered = sorted(calls.values(), key=lambda c: (c[0], c[1]["native_call_id"]))
    return [call for _, call in ordered]


def _billed_lines_by_turn(turns: list[dict], sidechains: list[dict]) -> dict[int, list[dict]]:
    """Assign all fragments of a request once, before summing any turn's usage."""
    requests: dict[str, tuple[int, list[dict]]] = {}
    for turn_num, lines in [
        *((i, turn["assistants"]) for i, turn in enumerate(turns, 1)),
        (0, sidechains),
    ]:
        for line in lines:
            if line.get("type") == "assistant":
                message = line.get("message", {})
                usage = message.get("usage") or {}
                if (
                    message.get("model") == "<synthetic>"
                    and not any(
                        usage.get(key, 0)
                        for key in (
                            "input_tokens",
                            "output_tokens",
                            "cache_creation_input_tokens",
                            "cache_read_input_tokens",
                        )
                    )
                    and not any((usage.get("cache_creation") or {}).values())
                    and not any((usage.get("server_tool_use") or {}).values())
                ):
                    # Harness markers retain their turn/content, but are not API
                    # requests. An unpriced marker would block the whole session's bill.
                    continue
                requests.setdefault(_message_key(line), (turn_num, []))[1].append(line)
    requests = _expand_fallback_requests(requests)
    starts = [safe_parse_dt((turn.get("user") or {}).get("timestamp")) for turn in turns]
    billed: dict[int, list[dict]] = {}
    for message_id, (turn_num, fragments) in requests.items():
        # One message id legitimately reaches this loop from SEVERAL files, so
        # only genuinely irreconcilable facts about the call may raise.
        # `LocalSource` folds a session's `subagents/agent-<id>.jsonl` files into
        # the parent transcript, and Claude Code writes the same assistant line
        # into every file it belongs to:
        #   * a subagent's own call lands in the parent (`isSidechain: false`,
        #     no `agentId`) AND in that subagent's file (`isSidechain: true`,
        #     its own `agentId`);
        #   * a call that spawns N subagents in parallel lands in the parent and
        #     in ALL N files, each stamping a DIFFERENT `agentId` — one billed
        #     call, N+1 copies, N distinct subagent ids.
        # Both shapes are duplication, not corruption: the copies share one
        # `message.id` and one `requestId`, and the token merge in
        # `parse_claude_code` is a per-message-id `max()` per component, so they
        # collapse to a single billed call exactly like streamed fragments do.
        # Raising on them ("parent ownership" / "subagent id") would abort the
        # WHOLE session's parse and drop every day it touched from the bill.
        # Model and request id still raise: those cannot legitimately differ
        # for one message id.
        metadata = {
            "model": {model_id_or_none(line.get("message", {}).get("model")) for line in fragments},
            "request id": {line.get("requestId") for line in fragments},
        }
        for field, values in metadata.items():
            if len(values - {None, ""}) > 1:
                raise ValueError(f"Conflicting {field} for Claude message {message_id}")
        # The parent transcript is authoritative for ownership: a call recorded
        # there belongs to the turn it was found in, and the subagent-file copies
        # are echoes. Only a call that appears NOWHERE but in sidechain files is
        # re-dated onto the turn that spawned it. `all` (not `any`) preserves
        # exactly what the old `fragments[0]` test did — parent turns are grouped
        # before sidechains above, so fragment 0 was the parent copy whenever one
        # existed — without depending on that ordering.
        if fragments and all(line.get("isSidechain") is True for line in fragments):
            instants = [safe_parse_dt(line.get("timestamp")) for line in fragments]
            at = min((instant for instant in instants if instant is not None), default=None)
            turn_num = _turn_index_for(at, starts)
        billed.setdefault(turn_num, []).extend(fragments)
    return billed


# Fields the parser indexes or hashes on, per record / per message / per content
# block / per usage dict. A newer or damaged transcript can retype any of them.
_LINE_STR_FIELDS = ("uuid", "requestId", "sessionId", "cwd", "version", "entrypoint")
_MESSAGE_STR_FIELDS = ("id", "model", "stop_reason")
_BLOCK_STR_FIELDS = ("type", "text", "id", "name", "tool_use_id")
_USAGE_COUNTS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _sanitize_usage(usage: dict) -> None:
    for key in _USAGE_COUNTS:
        if key in usage and type(usage[key]) is not int:
            del usage[key]
    for key in ("cache_creation", "server_tool_use"):
        nested = usage.get(key)
        if isinstance(nested, dict):
            for count in [k for k, v in nested.items() if type(v) is not int]:
                del nested[count]
        elif key in usage:
            del usage[key]


def _sanitize_line(line: dict) -> bool:
    """Drop wrong-typed fields from one record; False when the record is unreadable.

    A retyped value would raise deep inside the span build and lose the whole
    session, so it is removed (read as absent) instead.
    """
    drop_untyped(line, _LINE_STR_FIELDS, str)
    if "message" not in line:
        return True
    message = line["message"]
    if not isinstance(message, dict):
        return False
    drop_untyped(message, _MESSAGE_STR_FIELDS, str)
    drop_untyped(message, ("content",), (str, list))
    drop_untyped(message, ("usage",), dict)
    if "usage" in message:
        _sanitize_usage(message["usage"])
    if isinstance(message.get("content"), list):
        message["content"] = [b for b in message["content"] if isinstance(b, (dict, str))]
        for block in message["content"]:
            if isinstance(block, dict):
                drop_untyped(block, _BLOCK_STR_FIELDS, str)
    return True


def parse_claude_code(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Claude Code JSONL file into a span tree.

    Args:
        data: raw JSONL bytes.
        trace_id: the source-prefixed session id (e.g. `local:claude-code:<uuid>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project slug resolved by `LocalSource`.

    Returns:
        `(trace, all_spans)` with the trace's `spans` matching the list.
    """
    transcript_bytes = len(data)
    lines = [line for line in parse_lines(data) if _sanitize_line(line)]
    native_edit_results = result_patches(lines)
    if not lines:
        now = datetime.now(UTC)
        trace = TraceData(trace_id=trace_id, start_time=now, end_time=now)
        return trace, []

    # Subagent (Agent tool) traffic. Claude Code writes each subagent's
    # conversation to `<session-id>/subagents/agent-<id>.jsonl` (older builds
    # inlined it), every line stamped `isSidechain: true`; `LocalSource`
    # appends those files to the parent transcript. Their user prompts are
    # the PARENT's tool calls, not developer turns, so they never open a
    # turn -- but their assistant lines are real, billed API calls and are
    # folded into the turn that spawned them (by time) below, so the session's
    # totals include subagent spend.
    sidechain_lines = [line for line in lines if line.get("isSidechain") is True]
    main_lines = [line for line in lines if line.get("isSidechain") is not True]
    turns = _merge_replayed_turns(group_into_turns(main_lines))
    billed_by_turn = _billed_lines_by_turn(turns, sidechain_lines)
    all_spans: list[SpanData] = []
    root_id = f"{trace_id}:root"

    # Best-effort harness metadata.
    model_name = ""
    agent_version = ""
    effort_level = ""
    adaptive_thinking_disabled = False
    # Parent lines only: a subagent's model must not seed the session model.
    for line in main_lines:
        if not agent_version and line.get("version"):
            agent_version = line["version"]
        thinking_meta = line.get("thinkingMetadata")
        if thinking_meta and not effort_level:
            effort_level = thinking_meta.get("level", "")
            adaptive_thinking_disabled = bool(thinking_meta.get("disabled", False))
        if line.get("type") == "assistant":
            model_id = model_id_or_none(line.get("message", {}).get("model"))
            if model_id:
                model_name = model_id
        if model_name and agent_version and effort_level:
            break

    # Native session metadata Claude Code stamps on (nearly) every line:
    # `sessionId` (the agent's own session UUID), `permissionMode`
    # (default | acceptEdits | bypassPermissions | plan), `entrypoint`
    # (cli | sdk-py | sdk-ts | ...), and the per-line `isSidechain` flag
    # marking subagent (Task tool) traffic. First non-empty value wins for
    # the scalars; `sidechain_count` needs the full pass, so this loop
    # never breaks early. All land on the root span as `harness.*` attrs.
    agent_session_id = ""
    permission_mode = ""
    entrypoint = ""
    sidechain_count = 0
    for line in lines:
        if not agent_session_id and line.get("sessionId"):
            agent_session_id = line["sessionId"]
        if not permission_mode and line.get("permissionMode"):
            permission_mode = line["permissionMode"]
        if not entrypoint and line.get("entrypoint"):
            entrypoint = line["entrypoint"]
        if line.get("isSidechain") is True:
            sidechain_count += 1

    all_timestamps = [parse_dt(line["timestamp"]) for line in lines if line.get("timestamp")]
    if all_timestamps:
        session_start = min(all_timestamps)
        session_end = max(all_timestamps)
    else:
        session_start = datetime.now(UTC)
        session_end = session_start

    for turn_num, turn in enumerate(turns, 1):
        turn_id = f"{trace_id}:turn:{turn_num}"
        user_line = turn.get("user")
        assistant_lines = turn.get("assistants", [])
        tool_results = turn.get("tool_results", {})

        turn_start = (
            parse_dt(user_line["timestamp"])
            if user_line and "timestamp" in user_line
            else session_start
        )
        turn_end = turn_start
        for aline in assistant_lines:
            if aline.get("timestamp"):
                turn_end = max(turn_end, parse_dt(aline["timestamp"]))

        # Streaming fragments share message IDs — take the max per ID.
        # Track each component (fresh input, cache write incl. its 5m/1h
        # TTL split, cache read, output) separately so the $-calc has the
        # breakdown it needs — 1h writes bill at 2x input vs 1.25x for 5m,
        # and real Claude Code transcripts are 1h-dominated.
        msg_tokens: dict[str, tuple[int, int, int, int, int, int]] = {}
        # server-side web search/fetch REQUEST counts (never a
        # fee -- unpriced, display/count only). Tracked separately from
        # msg_tokens rather than widening that tuple: this isn't a billed
        # token component and has no place in the $-calc it feeds. Real shape
        # verified against local Claude Code transcripts:
        # `usage.server_tool_use = {"web_search_requests": N, "web_fetch_requests": N}`.
        # Same max-per-fragment dedup as msg_tokens: a later streaming
        # fragment of the same message reports the same-or-larger cumulative
        # count, never a fresh one to add.
        msg_web: dict[str, tuple[int, int]] = {}
        # the per-call cost data, keyed the same way
        # as msg_tokens (dedupe on message.id) so the two stay in lockstep.
        # `timestamp`/`request_id`/`model`/`service_tier` come from the
        # EARLIEST line for a message (a streaming fragment's first line is
        # when the call actually started); `latest_timestamp` retains a
        # distinct later fragment as directly-observed completion evidence.
        # Token totals above still take the MAX per component across
        # fragments, an unrelated dedupe axis.
        msg_meta: dict[str, dict[str, Any]] = {}
        # Parent calls first, then this turn's subagent calls. These are NOT
        # always distinct message ids: one subagent call is written to both the
        # parent transcript and the subagent's own file, so the same id arrives
        # twice once `LocalSource` folds them together. Merging the pair is the
        # point — the per-id `max()` below bills it once.
        billed_lines = billed_by_turn.get(turn_num, [])
        for aline in billed_lines:
            is_subagent = aline.get("isSidechain") is True
            msg = aline.get("message", {})
            usage = msg.get("usage", {})
            input_fresh = usage.get("input_tokens", 0)
            cache_w = usage.get("cache_creation_input_tokens", 0)
            # Nested TTL split (`usage.cache_creation.ephemeral_{5m,1h}_...`).
            # Older transcripts carry only the top-level total: the split
            # stays 0/0 and the $-calc bills every write at the 5m rate —
            # a deliberate lower bound, since the TTL is unknowable there.
            nested = usage.get("cache_creation") or {}
            cache_w_5m = nested.get("ephemeral_5m_input_tokens", 0)
            cache_w_1h = nested.get("ephemeral_1h_input_tokens", 0)
            if not cache_w and (cache_w_5m or cache_w_1h):
                cache_w = cache_w_5m + cache_w_1h
            cache_r = usage.get("cache_read_input_tokens", 0)
            completion_t = usage.get("output_tokens", 0)
            key = _message_key(aline)
            existing = msg_tokens.get(key, (0, 0, 0, 0, 0, 0))
            msg_tokens[key] = (
                max(existing[0], input_fresh),
                max(existing[1], cache_w),
                max(existing[2], cache_w_5m),
                max(existing[3], cache_w_1h),
                max(existing[4], cache_r),
                max(existing[5], completion_t),
            )
            server_tool_use = usage.get("server_tool_use") or {}
            existing_web = msg_web.get(key, (0, 0))
            msg_web[key] = (
                max(existing_web[0], server_tool_use.get("web_search_requests", 0) or 0),
                max(existing_web[1], server_tool_use.get("web_fetch_requests", 0) or 0),
            )

            model_id = model_id_or_none(msg.get("model"))
            if model_id and not is_subagent:
                model_name = model_id

            aline_ts = aline.get("timestamp")
            prior_meta = msg_meta.get(key)
            # Compared as parsed instants, never raw strings: a bare string
            # compare both crashes when the FIRST fragment for a message has
            # no timestamp at all (comparing a
            # str to prior_meta's stored None raises TypeError) and, even
            # when both are present, can pick the wrong "earliest" fragment
            # across differing UTC offsets.
            prior_instant = safe_parse_dt(prior_meta.get("timestamp")) if prior_meta else None
            prior_latest = (
                safe_parse_dt(prior_meta.get("latest_timestamp")) if prior_meta else None
            )
            seen_instant = safe_parse_dt(aline_ts)
            if prior_meta is None or (
                seen_instant is not None and (prior_instant is None or seen_instant < prior_instant)
            ):
                latest_timestamp = (
                    prior_meta.get("latest_timestamp")
                    if prior_meta is not None and prior_latest is not None
                    else aline_ts
                )
                msg_meta[key] = {
                    "timestamp": aline_ts,
                    "latest_timestamp": latest_timestamp,
                    # Keep a request identity observed on any fragment while
                    # still allowing an earlier fragment to win the clock.
                    "request_id": aline.get("requestId") or (prior_meta or {}).get("request_id"),
                    # The earliest fragment wins the clock and request id; the
                    # model is whichever fragment named one -- a first fragment
                    # with no model must not price the call as unknown when a
                    # later fragment carries it.
                    "model": model_id or (prior_meta or {}).get("model"),
                    "service_tier": usage.get("service_tier"),
                    "speed": (prior_meta or {}).get("speed"),
                    "subagent_id": (aline.get("agentId") if is_subagent else None)
                    or (prior_meta or {}).get("subagent_id"),
                }
            else:
                if seen_instant is not None and (
                    prior_latest is None or seen_instant > prior_latest
                ):
                    prior_meta["latest_timestamp"] = aline_ts
                if model_id and not prior_meta.get("model"):
                    prior_meta["model"] = model_id
                if aline.get("requestId") and not prior_meta.get("request_id"):
                    prior_meta["request_id"] = aline["requestId"]
                if is_subagent and aline.get("agentId") and not prior_meta.get("subagent_id"):
                    # LocalSource appends child files after the parent. A
                    # duplicate parent message can therefore be materialized
                    # before its sidechain copy; preserve that later evidence.
                    prior_meta["subagent_id"] = aline["agentId"]
            speed = usage.get("speed")
            if speed is not None:
                prior_speed = msg_meta[key].get("speed")
                if prior_speed is not None and prior_speed != speed:
                    raise ValueError("Conflicting response speed for Claude request")
                msg_meta[key]["speed"] = speed

        # Context window size = max across message IDs (what the model
        # "sees" at any one time). Total billed = sum (what you pay for).
        # `prompt` is everything sent: fresh input + cache write + cache read.
        per_msg_prompt = [t[0] + t[1] + t[4] for t in msg_tokens.values()]
        context_window_prompt = max(per_msg_prompt, default=0)
        total_billed_prompt = sum(per_msg_prompt)
        total_input_fresh = sum(t[0] for t in msg_tokens.values())
        total_cache_write = sum(t[1] for t in msg_tokens.values())
        total_cache_write_5m = sum(t[2] for t in msg_tokens.values())
        total_cache_write_1h = sum(t[3] for t in msg_tokens.values())
        total_cache_read = sum(t[4] for t in msg_tokens.values())
        total_completion = sum(t[5] for t in msg_tokens.values())
        total_web_searches = sum(t[0] for t in msg_web.values())
        total_web_fetches = sum(t[1] for t in msg_web.values())

        # one row per API call (message.id), the request grain - kept
        # alongside the turn sums above, which stay unchanged by construction (same
        # msg_tokens, just not discarded after summing). A call with no
        # timestamp at all can't be placed and is skipped (never guessed).
        cost_events = []
        for key, tokens in msg_tokens.items():
            meta = msg_meta.get(key, {})
            if not meta.get("timestamp"):
                continue
            event: dict[str, Any] = {
                "timestamp": meta["timestamp"],
                "started_at": meta["timestamp"],
                "completed_at": None,
                "duration_ms": None,
                "message_id": key or None,
                "request_id": meta.get("request_id"),
                "model": meta.get("model"),
                "service_tier": meta.get("service_tier"),
                **({"speed": meta["speed"]} if meta.get("speed") is not None else {}),
                "cache_ttl_known": tokens[1] == tokens[2] + tokens[3],
                "input_tokens": tokens[0],
                # Preserve unsplit writes at the same 5m lower-bound rate
                # used by the turn-level cost calculator. Apply after the
                # streaming max merge, so a later TTL split isn't added twice.
                "cache_write_5m_tokens": tokens[2] + max(0, tokens[1] - tokens[2] - tokens[3]),
                "cache_write_1h_tokens": tokens[3],
                "cache_read_tokens": tokens[4],
                "output_tokens": tokens[5],
            }
            started = safe_parse_dt(meta.get("timestamp"))
            completed = safe_parse_dt(meta.get("latest_timestamp"))
            if started is not None and completed is not None and completed > started:
                event["completed_at"] = meta["latest_timestamp"]
                event["duration_ms"] = int((completed - started).total_seconds() * 1000)
            # Only stamped on subagent calls, so the shape of a parent call's
            # event (and every existing consumer of it) is unchanged.
            if meta.get("subagent_id"):
                event["subagent_id"] = meta["subagent_id"]
            cost_events.append(event)

        # Per-turn cwd — Claude Code stamps `cwd` on (nearly) every
        # user/assistant entry. One session can span multiple repos
        # (the dev `cd`s mid-session), so we record THIS turn's cwd on
        # the turn span so each turn can resolve to its own repo. Prefer the
        # user line's cwd, fall back to the first assistant line that carries one.
        turn_cwd = user_line.get("cwd") if user_line else None
        if not turn_cwd:
            for aline in assistant_lines:
                if aline.get("cwd"):
                    turn_cwd = aline["cwd"]
                    break

        user_query = ""
        if user_line:
            user_query = extract_user_content(user_line.get("message", {}))

        assistant_text_parts = []
        for aline in assistant_lines:
            msg = aline.get("message", {})
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    assistant_text_parts.append(block.get("text", ""))
        assistant_text = "\n".join(assistant_text_parts)

        turn_span = SpanData(
            span_id=turn_id,
            trace_id=trace_id,
            parent_id=root_id,
            name=f"Turn {turn_num}",
            span_kind="CHAIN",
            status_code="OK",
            start_time=turn_start,
            end_time=turn_end,
            attributes={
                "input.value": user_query[:2000] if user_query else "",
                "output.value": assistant_text[:2000] if assistant_text else "",
                "cwd": turn_cwd or "",
                "llm.token_count.prompt": context_window_prompt,
                "llm.token_count.completion": total_completion,
                "llm.token_count.total_billed_prompt": total_billed_prompt,
                "llm.token_count.input_fresh": total_input_fresh,
                "llm.token_count.cache_write": total_cache_write,
                "llm.token_count.cache_write_5m": total_cache_write_5m,
                "llm.token_count.cache_write_1h": total_cache_write_1h,
                "llm.token_count.cache_read": total_cache_read,
                "llm.model_name": model_name,
                # request counts, never a fee -- see msg_web above.
                "llm.web_searches": total_web_searches,
                "llm.web_fetches": total_web_fetches,

                "llm.cost_events": cost_events,
            },
        )
        # transcript-anchored turn identity — the byte offset of
        # the line that OPENED the original turn. Replay merging preserves
        # this anchor instead of identifying a turn by its mutable token totals.
        _anchor_line = (
            user_line
            if user_line is not None
            else (assistant_lines[0] if assistant_lines else None)
        )
        _anchor_offset = _anchor_line.get(SOURCE_OFFSET_KEY) if _anchor_line is not None else None
        if _anchor_offset is not None:
            turn_span.attributes["source.byte_offset"] = _anchor_offset
        # the source anchor block - kind + content hash on every span.
        turn_span.attributes.update(
            {k: v for k, v in source_attrs(_anchor_line).items() if k != "source.byte_offset"}
        )
        all_spans.append(turn_span)

        for aline in assistant_lines:
            msg = aline.get("message", {})
            content_blocks = msg.get("content", [])
            if not isinstance(content_blocks, list):
                continue
            aline_ts = aline.get("timestamp")
            tool_start = parse_dt(aline_ts) if aline_ts else turn_start

            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue

                tool_use_id = block.get("id", "")
                tool_name = block.get("name", "unknown")
                if not isinstance(tool_name, str) or not tool_name:
                    tool_name = "unknown"
                tool_input = block.get("input", {})

                result_block = tool_results.get(tool_use_id, {})
                tool_output = result_block.get("content", "")
                is_error = result_block.get("is_error", False)

                tool_end = tool_start
                result_ts = turn.get("tool_result_timestamps", {}).get(tool_use_id)
                if result_ts:
                    tool_end = parse_dt(result_ts)

                input_str = (
                    json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
                )
                command_attrs = creation_command_attributes(input_str, tool_name)
                output_str = str(tool_output) if tool_output else ""

                tool_span = SpanData(
                    span_id=f"{trace_id}:tool:{tool_use_id}",
                    trace_id=trace_id,
                    parent_id=turn_id,
                    name=tool_name,
                    span_kind="CHAIN",
                    status_code="ERROR" if is_error else "OK" if tool_use_id in tool_results else "UNSET",
                    start_time=tool_start,
                    end_time=tool_end,
                    attributes={
                        "tool.name": tool_name,
                        "cwd": aline.get("cwd") or turn_cwd or "",
                        "tool.description": (
                            tool_input.get("description", "")
                            if isinstance(tool_input, dict)
                            else ""
                        ),
                        "input.value": input_str[:2000],
                        # the Skill name survives a long `args` that
                        # truncates `input.value` into unparseable JSON.
                        **skill_call_attributes(tool_name, tool_input),
                        **command_attrs,
                        **creation_output_attributes(
                            tool_output, command_attrs.get("pr.create.provider"),
                            command_attrs.get("pr.create.repository")
                        ),
                        "code.edit_evidence": call_edit_evidence(
                            tool_name,
                            tool_input,
                            native_edit_results.get(tool_use_id),
                            has_native=tool_use_id in native_edit_results,
                            successful=tool_use_id in tool_results and not is_error,
                        ),
                        "output.value": output_str[:2000],
                        # content-anchored identity. tool_use_id is
                        # Anthropic's own `toolu_…` id — stable across
                        # re-parses by construction (it also seeds span_id
                        # above; carried explicitly so consumers never parse
                        # it back out of the span_id string).
                        "source.tool_use_id": tool_use_id,
                        # The assistant message that produced this tool call is
                        # Claude's direct request-to-tool association. Match
                        # the cost-event identity exactly.
                        "llm.request_fact_id": _fallback_tool_request_id(
                            aline,
                            _tool_request_id(
                                aline,
                                msg_meta,
                                msg_meta.get(_message_key(aline), {}).get("request_id")
                                or msg.get("id"),
                            ),
                        ),
                    },
                )
                _aline_offset = aline.get(SOURCE_OFFSET_KEY)
                if _aline_offset is not None:
                    tool_span.attributes["source.byte_offset"] = _aline_offset
                tool_span.attributes.update(
                    {k: v for k, v in source_attrs(aline).items() if k != "source.byte_offset"}
                )
                all_spans.append(tool_span)

    # Parent lines drive slash_commands/interrupts: those describe what the
    # DEVELOPER typed, and a subagent's synthetic "user" turn is the
    # parent's own tool call, not a developer action. Skills are different —
    # a subagent invoking one is still work the developer's own dispatch
    # caused, so those fold in from the sidechain lines too
    # (`fold_subagent_skills`): otherwise a skill an orchestrator-style
    # plugin dispatched to a subagent (rather than calling directly) would
    # never appear in "skills used" at all.
    #
    # One shared `seen_skill_tool_ids` set across both calls: a subagent's own Skill call is echoed verbatim into BOTH the
    # parent transcript (isSidechain: false) and its own file (isSidechain:
    # true), sharing one tool_use id — two independent fresh sets would each
    # count it once, and the fold below would then double it.
    seen_skill_tool_ids: set[str] = set()
    session_signals = extract_session_signals(main_lines, seen_skill_tool_ids)
    if sidechain_lines:
        session_signals = fold_subagent_skills(
            session_signals,
            extract_session_signals(sidechain_lines, seen_skill_tool_ids),
        )
    # Inline sidechain requests preceding a checkpoint are billing evidence.
    # Appended child files remain after the parent's checkpoints: their order
    # cannot establish which cumulative snapshot included those requests.
    cost_summary = extract_cost_summary(
        [
            line
            for line in lines
            if line.get("isSidechain") is not True or line.get("type") == "assistant"
        ],
        request_expander=_expand_fallback_requests,
    )

    subagent_edits = _subagent_edit_calls(
        trace_id, turns, main_lines, sidechain_lines, native_edit_results
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
        attributes={
            "user.id": user_id or "",
            "llm.model_name": model_name,
            "session.id": trace_id,
            "project.name": project_name or "",
            "harness.agent_session_id": agent_session_id,
            "harness.agent_version": agent_version,
            "harness.effort_level": effort_level,
            "harness.adaptive_thinking_disabled": adaptive_thinking_disabled,
            "harness.permission_mode": permission_mode,
            "harness.entrypoint": entrypoint,
            "harness.sidechain_count": sidechain_count,
            "harness.subagent_count": len(
                {line.get("agentId") for line in sidechain_lines if line.get("agentId")}
            ),
            "harness.session_signals": json.dumps(session_signals),
            **({"llm.cost_summary": cost_summary} if cost_summary is not None else {}),
            **({"code.subagent_edit_calls": subagent_edits} if subagent_edits else {}),
            # raw transcript length as read (pre-normalization).
            "source.transcript_bytes": transcript_bytes,
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


__all__ = ["parse_claude_code"]
