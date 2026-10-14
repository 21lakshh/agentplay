"""Parse a synthesized Cursor CLI/agent NDJSON transcript into the universal SpanData shape.

Cursor's CLI/agent surfaces persist each session as its own ``store.db`` SQLite
file (no per-session JSONL like Claude Code/Codex). Materialization decodes it
into an NDJSON transcript per session - a header line, then one
line per message verbatim (see ``agentplay.materialize.
find_cursor_store_candidates``):

    {"type": "cursor_store_session", "agentId": ..., "name": ..., "createdAt":
     <ms>, "updatedAt": <ms>, "surface": "cli", "timezone": ..., "cwd": ...,
     "contextUsed": ..., "contextWindow": ...}
    {"role": "user"|"assistant"|"tool", "content": <str|parts>, "id": ...,
     "providerOptions": ...}
    ...

``content`` is a plain string or a list of parts (``text`` /
``redacted-reasoning``/``reasoning`` / ``tool-call`` / ``tool-result`` /
``image``). The model lives at ``providerOptions.cursor.modelName`` (message-
or part-level). Unlike every other local format, Cursor carries no native
per-turn billed token usage, so turn attrs default to 0 (see ``_open_turn``).
When the header carries billed usage for the session, it
also carries a SESSION-WIDE billed total (``prompt_tokens``/
``completion_tokens``/``cache_read_tokens``/``cache_write_tokens``/
``cost_cents``) — this parser stamps those onto the root span (see
``_session_billed_usage_attrs``), where session aggregation reads them.

Per-event billed usage (newer materializations): right after the header,
one line is appended per CHARGED usage event of the matched
conversation, sorted ascending by timestamp:

    {"type": "cursor_usage_event", "ts": <epoch millis int>,
     "input_tokens": int, "output_tokens": int, "cache_read_tokens": int,
     "cache_write_tokens": int, "cost_cents": float, "model": str}

This parser maps those events onto turns and stamps the per-turn sums as
``llm.token_count.prompt`` (input + both cache buckets, matching the
header's prompt convention), ``.completion``, ``.cache_read``,
``.cache_write``, ``.input_fresh`` (codex formula: prompt − cache_read) and
``llm.cost.billed_cents`` (exact billed cents for the turn, preferred over
any pricing-catalog estimate).

Event→turn mapping strategy (in preference order):
  1. TIMESTAMP CONTAINMENT — used when every turn-opening message carries a
     parseable ``createdAt`` (ISO-8601 string; forwarded from IDE
     composer bubbles — exactly the transcripts where the usage join lands)
     and those times are non-decreasing. An event belongs to the last turn
     whose opening time <= the event's ``ts`` (events bill when a generation
     finishes, i.e. after the user message that started the turn); events
     stamped before the first turn clamp to turn 1.
  2. ORDER-PROPORTIONAL fallback — when message timestamps are absent
     (store.db CLI/ACP transcripts, older materializations). Event ``j`` of ``n``
     (0-based, ts-ascending) goes to turn ``min(k-1, j*k // n)`` of ``k``:
     exact 1:1 when ``n == k``, everything to the single turn when
     ``k == 1``, an even order-based spread otherwise. FAILURE MODE: when
     one turn fired multiple billed calls and a neighbour fired none, this
     smears usage across adjacent turns — per-turn numbers are then an
     approximation, though session totals stay exact (the header sums are
     authoritative and land on the root span regardless).

Turn/tool span times use a message's real ``createdAt`` when available, so
request attribution uses the same clock as the parser's usage mapping.
Undated messages retain the synthetic spread across the header window; their
turn attribution remains approximate.

Returns the same span shape/contract as ``opencode.py``/``codex.py``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import UTC, datetime
import hashlib
import json
import logging

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._paths import basename as path_basename
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    _SKILL_BODY_MAX_CHARS,
    SOURCE_KIND_SQLITE,
    SOURCE_ROW_CURSOR_KEY,
)
from agentplay._core.ingestion.coding_agent.produced import (
    creation_command_attributes,
    creation_output_attributes,
)
from agentplay._core.ingestion.coding_agent.skill_names import skill_name_from_skill_md_path
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData
from agentplay._core.ingestion.coding_agent.tool_taxonomy import aliases_for

logger = logging.getLogger(__name__)

# input.value / output.value truncation, matching every other local parser.
_TRUNCATE = 2000

# Raw -> canonical tool names live in the shared table (tool_taxonomy.py).
_TOOL_ALIASES: dict[str, str] = aliases_for("cursor")


def _skill_body_from_result(result: dict | None) -> str:
    """Best-effort SKILL.md body from a Read tool-result part; "" if unavailable.

    The part's own
    ``"result"`` value is a JSON-encoded string wrapping ``{"contents": "..."}``.
    Handle a plain dict too (some materializations may skip the string-encoding
    step) since either shape is plausible.

    A THIRD shape also occurs: ``"result"`` as a plain
    string that is the file's literal content, not JSON at all (e.g. the file
    starts with ``---`` frontmatter, which is invalid JSON). The caller only
    reaches this function after confirming the read targeted a SKILL.md file,
    so a string that fails to parse as JSON is the body itself, not garbage —
    treating it as empty would drop that whole shape. Never raises -
    a body is a nice-to-have; a parse failure here must not break the span.
    """
    if not isinstance(result, dict):
        return ""
    raw = result.get("result", "")
    if isinstance(raw, dict):
        contents = raw.get("contents")
        return contents if isinstance(contents, str) else ""
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw
        if isinstance(parsed, dict):
            contents = parsed.get("contents")
            return contents if isinstance(contents, str) else ""
        return ""
    return ""


# Lives in `skill_names` so every caller derives names through the SAME
# function - two copies of this rule is exactly how two names for one skill
# come to differ. Kept as a module-local alias: this parser and its tests
# reference it by the private name, and the indirection costs nothing.
_skill_name_from_skill_md_path = skill_name_from_skill_md_path


def _skill_read_from_tool(
    tool_name: str, args: dict, result: dict | None
) -> tuple[str, str] | None:
    """Detect a Cursor skill invocation from an already-resolved Read call.

    Cursor has no dedicated ``Skill``/``activate_skill`` tool — that vocabulary
    is Claude Code's. Cursor's own skills system is invoked by the model
    reading the skill's SKILL.md file with a plain Read call, per Cursor's own
    injected instructions ("read the skill file at the provided absolute path
    using the Read tool"). Confirmed against this machine's real local Cursor
    IDE history — the tool name in that capture was
    ``read_file_v2``, already aliased to ``"Read"`` by ``_TOOL_ALIASES`` above.

    Matches on the file's basename only (case-insensitive) via
    `_skill_name_from_skill_md_path` — see that function for why a fixed root
    directory doesn't work here.

    Args:
        tool_name: the tool name AFTER `_TOOL_ALIASES` resolution (e.g. "Read",
            not "read_file_v2").
        args: the tool call's args, already passed through
            `_normalize_file_path_arg` (so `"file_path"` is present when
            resolvable).
        result: the matching tool-result part, or None if the call never
            completed.

    Returns:
        `(canonical_skill_name, body)` when this call read a SKILL.md file
        with a resolvable canonical name (`body` is `""` when the result
        carried no usable content, INCLUDING a failed read — see below);
        `None` when this isn't a skill read.
    """
    if tool_name != "Read":
        return None
    file_path = args.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if path_basename(file_path).lower() != "skill.md":
        return None
    canonical = _skill_name_from_skill_md_path(file_path)
    if canonical is None:
        return None
    # A failed read (file missing, permission denied, ...) still names the
    # attempted skill, but its "result" is an error string/blob, not file
    # content — post-, a non-JSON string is otherwise treated as the
    # literal body, so an unguarded call here would store the error message
    # as the skill's body. Same empty-body contract as a dangling call with
    # no result at all.
    if result is not None and _tool_result_is_error(result):
        return canonical, ""
    return canonical, _skill_body_from_result(result)


def _event_hash(event: dict | None) -> str | None:
    """Content-anchored identity for one raw NDJSON event.

    Cursor's cache file is REGENERATED on every materialization (the whole
    NDJSON is rewritten), so byte offsets are not stable between reads —
    unlike claude-code's append-only JSONL. A canonical-JSON hash of the
    event dict is the stable identity instead: the same message content
    hashes the same regardless of where the rewrite placed it. 16 hex chars
    of sha1 — collision space is per-session (tens to hundreds of events).
    """
    if not isinstance(event, dict):
        return None
    # The parser's own `_ent_*` stamps (the bubble id the synthesizer adds
    # as the row cursor) are not content - stripping them keeps every hash
    # identical to what the same message hashed to before the stamp existed.
    body = {k: v for k, v in event.items() if not (isinstance(k, str) and k.startswith("_ent_"))}
    try:
        canonical = json.dumps(body, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _row_cursor(event: dict | None) -> str | None:
    """The record's addressable key — the composer bubble id.

    The synthesizer has always written the bubble id as the message's
    ``id`` (user and assistant messages), so existing files already carry
    it and it is already inside the content hash. The ``_ent_row_cursor``
    stamp is the generic fallback for a producer that keeps the key out of
    the record body. None when neither is present.
    """
    if not isinstance(event, dict):
        return None
    for key in ("id", SOURCE_ROW_CURSOR_KEY):
        cursor = event.get(key)
        if isinstance(cursor, str) and cursor:
            return cursor
    return None


def parse_cursor_store(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a synthesized Cursor CLI/agent NDJSON transcript into a span tree.

    Args:
        data: raw NDJSON bytes — header line + one message line each.
        trace_id: the source-prefixed session id (e.g. `local:cursor:<uuid>`).
        user_id: identifier to stash on the root span's `user.id`.
        project_name: project name resolved by `LocalSource` (from the
            header's `cwd`); used as-is when the caller already knows it.

    Returns:
        `(trace, all_spans)` with the trace's `spans` matching the list.
        Empty when the header is missing/malformed or there are zero
        messages — `LocalSource` already skips indexing such a file (see
        `_index_cursor_store_file`), so this is a defensive fallback for
        direct callers, not a path normal indexing hits.
    """
    transcript_bytes = len(data)
    header, usage_events, messages = _parse_ndjson(data, trace_id)
    if header is None or not messages:
        now = datetime.now(UTC)
        return TraceData(trace_id=trace_id, start_time=now, end_time=now), []

    cwd = header.get("cwd") if isinstance(header.get("cwd"), str) else ""
    if not project_name and cwd:
        project_name = path_basename(cwd) or None

    session_start = _ms_to_dt(header.get("createdAt"))
    session_end = _ms_to_dt(header.get("updatedAt"))
    if session_start is None and session_end is None:
        session_start = session_end = datetime.now(UTC)
    elif session_start is None:
        session_start = session_end
    elif session_end is None or session_end < session_start:
        session_end = session_start

    n_messages = len(messages)

    def _clock(i: int) -> datetime:
        real = _iso_to_dt(messages[i].get("createdAt"))
        if real is not None:
            return real
        if n_messages <= 1:
            return session_start
        return session_start + (session_end - session_start) * (i / (n_messages - 1))

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []
    # toolCallId -> (tool-call part, owning turn's span_id, call timestamp,
    # the requesting message's row cursor or None).
    pending_calls: dict[str, tuple[dict, str, datetime, str | None]] = {}
    # skill invocations detected via `_skill_read_from_tool`, keyed by
    # canonical name — same contract shape claude_code.py's capture produces.
    skill_counts: Counter[str] = Counter()
    skill_bodies: dict[str, str] = {}

    turn_num = 0
    turn: SpanData | None = None
    turn_output_parts: list[str] = []
    turn_model = ""
    # Parallel per-turn records for usage-event mapping: the turn spans in
    # order, and the REAL wall-clock time of each turn's opening message
    # (None when that message carried no createdAt) — see module docstring.
    turns: list[SpanData] = []
    turn_open_real_ts: list[datetime | None] = []
    seen_unknown_roles: set[str] = set()

    def _close_turn() -> None:
        nonlocal turn, turn_output_parts, turn_model
        if turn is not None:
            turn.attributes["output.value"] = "\n".join(turn_output_parts)[:_TRUNCATE]
            if turn_model:
                turn.attributes["llm.model_name"] = turn_model
        turn = None
        turn_output_parts = []
        turn_model = ""

    def _open_turn(
        ts: datetime,
        input_value: str = "",
        real_ts: datetime | None = None,
        source_hash: str | None = None,
        row_cursor: str | None = None,
    ) -> None:
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
                **creation_command_attributes(input_value),
                "output.value": "",
                # Cursor records cwd once on the header, not per message, so
                # every turn gets the same value — mirrors Codex's uniform
                # per-turn cwd (see codex.py), so the whole session resolves
                # to one repo.
                "cwd": cwd,
                # Token attrs default to 0 (no NATIVE per-turn usage in this
                # format). Newer materializations carry cursor_usage_event
                # lines; when present, the
                # mapped turns get these overwritten by
                # _stamp_turn_billed_usage after the message loop. (A
                # session-WIDE total may also land on the root span — see
                # _session_billed_usage_attrs.)
                "llm.token_count.prompt": 0,
                "llm.token_count.completion": 0,
                "llm.token_count.total_billed_prompt": 0,
                "llm.token_count.input_fresh": 0,
                "llm.token_count.cache_read": 0,
                "llm.token_count.cache_write": 0,
                "llm.model_name": "",
            },
        )
        if source_hash is not None:
            # content-anchored turn identity (offsets are unstable
            # here — see _event_hash's docstring).
            turn.attributes["source.content_hash"] = source_hash
        # the store file is rewritten on each materialization, so the anchor kind is
        # the content hash, never an offset; `source.kind` says so explicitly.
        turn.attributes["source.kind"] = SOURCE_KIND_SQLITE
        if row_cursor:
            # the bubble id — the seekable key in a rewritten store.
            turn.attributes["source.row_cursor"] = row_cursor
        all_spans.append(turn)
        turns.append(turn)
        turn_open_real_ts.append(real_ts)

    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "system":
            continue
        ts = _clock(i)

        if role == "user":
            _close_turn()
            _open_turn(
                ts,
                _text_from_content(msg.get("content")),
                _iso_to_dt(msg.get("createdAt")),
                source_hash=_event_hash(msg),
                row_cursor=_row_cursor(msg),
            )
            continue

        if turn is None:
            # Assistant/tool activity before any user message (e.g. an
            # agent-initiated step) still needs a turn to live under.
            _open_turn(
                ts,
                real_ts=_iso_to_dt(msg.get("createdAt")),
                source_hash=_event_hash(msg),
                row_cursor=_row_cursor(msg),
            )
        turn.end_time = max(turn.end_time, ts)

        if role == "assistant":
            parts = msg.get("content") if isinstance(msg.get("content"), list) else []
            text_bits = [
                p["text"]
                for p in parts
                if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
            ]
            if text_bits:
                turn_output_parts.append("\n".join(text_bits))
            model = _model_from_message(msg)
            if model:
                turn_model = model
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "tool-call":
                    call_id = part.get("toolCallId")
                    if call_id and isinstance(call_id, str):
                        previous = pending_calls.get(call_id)
                        if previous is not None:
                            old_call, old_turn, old_ts, old_cursor = previous
                            # Cursor reuses call IDs in distinct bubbles. Keep
                            # the unfinished attempt before the newer call takes
                            # ownership of any subsequent result.
                            all_spans.append(
                                _build_tool_span(
                                    trace_id,
                                    old_turn,
                                    old_call,
                                    None,
                                    old_ts,
                                    old_ts,
                                    skill_counts,
                                    skill_bodies,
                                    row_cursor=old_cursor,
                                )
                            )
                        pending_calls[call_id] = (part, turn.span_id, ts, _row_cursor(msg))
        elif role == "tool":
            parts = msg.get("content") if isinstance(msg.get("content"), list) else []
            for part in parts:
                if not (isinstance(part, dict) and part.get("type") == "tool-result"):
                    continue
                call_id = part.get("toolCallId")
                call_part, tool_turn_id, call_ts, call_cursor = pending_calls.pop(
                    call_id if isinstance(call_id, str) else None, (None, turn.span_id, ts, None)
                )
                all_spans.append(
                    _build_tool_span(
                        trace_id,
                        tool_turn_id,
                        call_part,
                        part,
                        call_ts,
                        ts,
                        skill_counts,
                        skill_bodies,
                        # The request's message anchors the call; the result's
                        # message is the fallback for an orphaned result.
                        row_cursor=call_cursor or _row_cursor(msg),
                    )
                )
        else:
            # A role outside user/assistant/tool/system drops the whole
            # message's content with nothing else to show for it.
            # `role` is whatever the vendor put there, so the dedup key is
            # built from its type name and repr rather than the value: repr is
            # defined for every object (a list or dict is unhashable and using
            # the value itself as a set key would raise TypeError and abort the
            # whole transcript, the exact failure this warning exists to
            # prevent), and the type name keeps values Python calls equal but a
            # reader would not -- integer 0 and boolean false -- each named once.
            role_key = (type(role).__name__, repr(role))
            if role_key not in seen_unknown_roles:
                seen_unknown_roles.add(role_key)
                logger.debug(
                    "session %s: cursor_store message has unrecognised role %r; "
                    "message content dropped",
                    trace_id,
                    role,
                )

    _close_turn()

    if usage_events and turns:
        _stamp_turn_billed_usage(turns, turn_open_real_ts, usage_events)

    # Tool calls that never saw a matching result (session ended mid-call) —
    # still worth a span; the absence of a result does not establish success.
    for call_part, tool_turn_id, call_ts, call_cursor in pending_calls.values():
        all_spans.append(
            _build_tool_span(
                trace_id,
                tool_turn_id,
                call_part,
                None,
                call_ts,
                call_ts,
                skill_counts,
                skill_bodies,
                row_cursor=call_cursor,
            )
        )

    # Session model = LAST model used, matching opencode/codex/claude_code —
    # consumers read root llm.model_name for provider/cost attribution, and
    # the last model is what siblings report after a mid-session switch.
    # Fallback: the file HEADER also records the picker's model (e.g.
    # "cursor-grok-4.6-high-fast"), and some sessions carry NO message-level
    # providerOptions at all - those
    # sessions would otherwise come out model-less and unpriced despite the
    # header naming the model. "default" (Cursor Auto) passes through as-is: it is
    # honest, and pricing already treats it as unpriceable either way.
    session_model = _last_model(messages) or str(header.get("model") or "")

    # same session_signals contract claude_code.py populates, so
    # skill_distribution reads Cursor sessions the same way it reads
    # Claude Code ones. No "interrupts" key: build_sidecar_signals treats any
    # non-empty dict as a real signal (`if sig.get(key)`), so a hardcoded zero
    # would make every signal-less Cursor session look like it has interrupt
    # data, creating a sidecar for nothing. This format can't detect interrupts at all - omitting the key,
    # not zeroing it, is what "no signal source" actually means.
    signals = {
        "slash_commands": [],
        "skills_used": [{"path": p, "count": c} for p, c in skill_counts.most_common()],
        "skill_definitions": [
            {"name": p, "body": skill_bodies[p]}
            for p, _ in skill_counts.most_common()
            if skill_bodies.get(p)
        ],
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
            "llm.model_name": session_model,
            "session.id": trace_id,
            "project.name": project_name or "",
            # Cursor's native session id — the header's `agentId`. The
            # header carries no agent/CLI version, so that attr is absent.
            "harness.agent_session_id": str(header.get("agentId") or ""),
            "harness.session_signals": json.dumps(signals),
            **_session_billed_usage_attrs(header),
            **_session_context_path_attrs(header),
            # length of the synthesized NDJSON as parsed. Weaker
            # than claude-code's (the file is rewritten per materialization)
            # but still an honest observed-size claim.
            "source.transcript_bytes": transcript_bytes,
        },
    )
    all_spans.insert(0, root_span)
    if messages and not any(_iso_to_dt(m.get("createdAt")) is not None for m in messages):
        # Every turn clock above came from `_clock`'s even spread between the
        # header's createdAt/updatedAt — synthetic ordering, not observed
        # activity time. Same flag cursor_agent_transcript raises for its
        # clock-less source, so consumers omit the fabricated dates. A composer with SOME real
        # createdAt stays observed: those anchor the spread.
        for span in all_spans:
            span.attributes["session.timestamps_observed"] = False

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
    skill_counts: Counter[str],
    skill_bodies: dict[str, str],
    row_cursor: str | None = None,
) -> SpanData:
    """One tool-call/tool-result pair -> a tool span.

    Tool names are aliased to the canonical Claude/Codex vocabulary via
    `_TOOL_ALIASES`. Every other local parser here passes tool names
    through unaliased because their native names ARE canonical; Cursor's
    aren't, so this is the one parser that needs the table. Unknown names
    pass through unchanged.

    Args are also normalized here (see `_normalize_file_path_arg`) so the
    shared `file_paths.py` extractor — which only ever matches a
    `"file_path"` key, on purpose, to stay byte-identical for Claude/
    Codex/opencode — finds Cursor's file too. This keeps the Cursor-only
    quirk (its args key the touched file as `"path"`/`"paths"`) confined
    to this parser instead of widening the shared extractor.

    `skill_counts`/`skill_bodies` are mutated in place when this call turns
    out to be a Cursor skill invocation (see `_skill_read_from_tool`)
    — done here rather than at the call sites so the tool-name-alias + arg-
    normalization logic this needs stays in one place.
    """
    raw_name = (call or {}).get("toolName")
    raw_name = raw_name if isinstance(raw_name, str) and raw_name else "unknown"
    tool_name = _TOOL_ALIASES.get(raw_name, raw_name)
    args = (call or {}).get("args", {})
    if isinstance(args, dict):
        args = _normalize_file_path_arg(args)
        skill = _skill_read_from_tool(tool_name, args, result)
        if skill is not None:
            canonical, body = skill
            skill_counts[canonical] += 1
            # Capped the same as the Claude Code path (`_skill_body_after_marker`)
            # — a Read result can embed arbitrarily large file content otherwise.
            body = body[:_SKILL_BODY_MAX_CHARS] if body else body
            if body and len(body) > len(skill_bodies.get(canonical, "")):
                skill_bodies[canonical] = body
    input_value = args if isinstance(args, str) else json.dumps(args)

    output_value = ""
    is_error = False
    if result is not None:
        raw_result = result.get("result", "")
        output_value = raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
        is_error = _tool_result_is_error(result)

    call_id = (call or {}).get("toolCallId") or f"{turn_id}:{start.isoformat()}"
    source_attrs: dict = {}
    native_call_id = (call or {}).get("toolCallId")
    if native_call_id:
        # Cursor's own toolCallId — content-anchored, survives the
        # cache rewrite (carried explicitly, same reasoning as claude-code's
        # source.tool_use_id).
        source_attrs["source.tool_call_id"] = str(native_call_id)
    content_hash = _event_hash(call) or _event_hash(result)
    if content_hash is not None:
        source_attrs["source.content_hash"] = content_hash
        source_attrs["source.kind"] = SOURCE_KIND_SQLITE
    if row_cursor:
        source_attrs["source.row_cursor"] = row_cursor
    # Native tool-call IDs repeat across IDE bubbles. The bubble identifies
    # the occurrence while the vendor ID above remains useful for correlation.
    occurrence_id = f"{call_id}:{row_cursor}" if row_cursor else call_id
    command_attrs = creation_command_attributes(input_value, tool_name)
    return SpanData(
        span_id=f"{trace_id}:tool:{occurrence_id}",
        trace_id=trace_id,
        parent_id=turn_id,
        name=tool_name,
        span_kind="CHAIN",
        status_code="UNSET" if result is None else ("ERROR" if is_error else "OK"),
        start_time=start,
        end_time=end,
        attributes={
            "tool.name": tool_name,
            "code.edit_evidence": extract_edit_evidence(
                "apply_patch" if raw_name == "ApplyPatch" else tool_name,
                input_value,
                successful=result is not None and not is_error,
            ),
            "input.value": input_value[:_TRUNCATE],
            **command_attrs,
            **creation_output_attributes(
                output_value,
                command_attrs.get("pr.create.provider"),
                command_attrs.get("pr.create.repository"),
            ),
            "output.value": output_value[:_TRUNCATE],
            **source_attrs,
        },
    )


def _normalize_file_path_arg(args: dict) -> dict:
    """Alias Cursor's `path`/`paths`/`targetFile`/`relativeWorkspacePath` arg
    keys to `file_path`.

    Cursor's Read/Edit/
    Grep/StrReplace key the touched file as `"path"`; ReadLints keys it as
    a `"paths"` list. IDE composer's `*_v2` tools use different keys entirely
    (`read_file_v2`/`edit_file_v2` calls never carry `"path"`/`"paths"`):
    `read_file_v2` (aliased to `"Read"`) carries `"targetFile"`
    (`"effectiveUri"` is also present but duplicates `"targetFile"`, so it is
    redundant to check separately) and `edit_file_v2` (aliased to `"Edit"`)
    carries `"relativeWorkspacePath"`. Without these, a
    `_TOOL_ALIASES`-resolved `"Read"`/`"Edit"` call never gets a `file_path`,
    which would break Cursor skill-name resolution AND every other
    `file_paths.py` consumer (read/edit/search counters, blind-edit-rate,
    file attribution) for the entire IDE composer surface. Every other local
    parser's native tool args already carry `"file_path"`, which is the only
    key `file_paths.py`'s shared extractor matches — so this is where
    Cursor's shape gets translated, not the shared module.

    Only fills `file_path` when it's absent, and never mutates/removes the
    original key — purely additive. Glob's `target_directory`/
    `targetDirectory` names a directory, not one file, so it's deliberately
    left unmapped rather than fabricating a `file_path` for it. Shell/`Bash`-
    style `"command"` strings are left untouched too — Cursor's shell calls
    get no `file_path`, matching how Claude's `Bash` calls are handled.

    `file_path` is PREPENDED, not appended: `_build_tool_span` serializes
    these args to JSON and truncates to `_TRUNCATE` chars for the span's
    `input.value`, which `file_paths.py`'s regex then scans — a key added at
    the end can be sheared off by that truncation before it's ever read.
    This matters in practice for `edit_file_v2`, whose args carry the whole
    edit diff under `streamingContent`, easily past `_TRUNCATE` on its own.

    Args:
        args: The tool-call's raw `args` dict (already confirmed a dict
            by the caller).

    Returns:
        `args` unchanged, or a shallow copy with `file_path` added.
    """
    if "file_path" in args:
        return args
    path = args.get("path")
    if isinstance(path, str) and path:
        return {"file_path": path, **args}
    paths = args.get("paths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0]:
        return {"file_path": paths[0], **args}
    target_file = args.get("targetFile")
    if isinstance(target_file, str) and target_file:
        return {"file_path": target_file, **args}
    relative_workspace_path = args.get("relativeWorkspacePath")
    if isinstance(relative_workspace_path, str) and relative_workspace_path:
        return {"file_path": relative_workspace_path, **args}
    return args


def _tool_result_is_error(result: dict) -> bool:
    """Best-effort error detection on a tool-result part."""
    status = result.get("status") or result.get("state")
    if isinstance(status, str) and ("error" in status.lower() or "fail" in status.lower()):
        return True
    return result.get("success") is False or bool(result.get("error") or result.get("isError"))


def _text_from_content(content: object) -> str:
    """Join every ``text``-type part; ``content`` may also be a plain string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            p["text"]
            for p in content
            if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
        )
    return ""


def _model_from_message(msg: dict) -> str | None:
    """First ``providerOptions.cursor.modelName`` on a message or its parts."""
    candidates = [msg.get("providerOptions")]
    content = msg.get("content")
    if isinstance(content, list):
        candidates.extend(p.get("providerOptions") for p in content if isinstance(p, dict))
    for po in candidates:
        if isinstance(po, dict):
            cursor = po.get("cursor")
            if isinstance(cursor, dict) and isinstance(cursor.get("modelName"), str):
                return cursor["modelName"]
    return None


def _last_model(messages: list[dict]) -> str | None:
    """Last model name used in the session, or None if unknown."""
    for msg in reversed(messages):
        model = _model_from_message(msg)
        if model:
            return model
    return None


# header key -> root-span attribute key, for the token fields (cost_cents is
# handled separately below since it converts units and lands under a
# different name).
_USAGE_TOKEN_FIELDS = (
    ("prompt_tokens", "session.token_count.prompt"),
    ("completion_tokens", "session.token_count.completion"),
    ("cache_read_tokens", "session.token_count.cache_read"),
    ("cache_write_tokens", "session.token_count.cache_write"),
)


def _session_billed_usage_attrs(header: dict) -> dict:
    """Session-wide billed-token/cost root-span attrs, when the header has them.

    The header may carry ``prompt_tokens``/``completion_tokens``/
    ``cache_read_tokens``/``cache_write_tokens``/``cost_cents`` - they're
    ABSENT, not zero, when no billed usage is known. Every value is validated numeric
    before use so a malformed/missing header can't crash the parse or
    smuggle in a false "session billed zero" reading. ``cost_cents``
    is checked independently of the token fields - it may lag behind
    (present tokens, absent cost) on older files.

    Returns:
        A dict to merge into the root span's ``attributes`` — empty when the
        header carries none of these fields. Keys are only present for
        fields that were actually found, so
        session aggregation can tell "no
        data" (key absent) from "session billed zero" (key present, 0).
    """
    attrs: dict = {}
    for header_key, attr_key in _USAGE_TOKEN_FIELDS:
        value = header.get(header_key)
        # Accept int OR float here (never bool, never NaN/inf, same guard the
        # cost_cents check below already uses) - the usage source can hand back
        # a float, and rejecting it drops the session's whole billed total.
        if (
            isinstance(value, int | float)
            and not isinstance(value, bool)
            and 0 <= value < float("inf")
        ):
            attrs[attr_key] = value
    cost_cents = header.get("cost_cents")
    if (
        isinstance(cost_cents, int | float)
        and not isinstance(cost_cents, bool)
        and 0 <= cost_cents < float("inf")
    ):
        attrs["session.cost_usd"] = cost_cents / 100.0
    return attrs


def _session_context_path_attrs(header: dict) -> dict:
    """Root-span attr carrying the composer's structured file paths, when present.

    Cursor's IDE composer records reference real files — ``newlyCreatedFiles``,
    ``context.selections``, the ``originalFileStates`` keys, and the git worktree
    / tracked-repo roots — but none of that is a tool call, so none of it reaches
    the tool spans that repo attribution normally reads. Materialization collects
    those paths into the header's ``contextFiles``
    (``agentplay.materialize._cursor_composer_context_paths``); this
    forwards them so they can be leaf-matched against known repos.

    Absent, not empty, when nothing was found - an older materialization omits
    the field entirely, and a reader must be able to tell "this build never collected
    paths" from "this session referenced no files".

    Encoded as a JSON string for the same reason ``harness.session_signals`` is:
    span attributes travel as scalars, and every reader of that attribute already
    accepts either a JSON string or a decoded value.

    Args:
        header: The ``cursor_store_session`` header dict (line 1 of the NDJSON).

    Returns:
        ``{"harness.context_files": "<json array>"}``, or ``{}`` when the header
        carries no usable paths.
    """
    raw = header.get("contextFiles")
    if not isinstance(raw, list):
        return {}
    paths = [p for p in raw if isinstance(p, str) and p]
    if not paths:
        return {}
    return {"harness.context_files": json.dumps(paths, separators=(",", ":"))}


def _parse_ndjson(
    data: bytes, trace_id: str = "<unknown>"
) -> tuple[dict | None, list[dict], list[dict]]:
    """Split the transcript into `(header, usage_events, messages)`.

    Line 1 must be the `cursor_store_session` header; every subsequent
    valid `{role: ...}` dict is a message, and every
    `{"type": "cursor_usage_event", "ts": <epoch ms int>, ...}` dict is a
    per-event billed-usage line (newer materializations - see module docstring).
    An event line without a positive-int `ts` is dropped: it can't be
    ordered or mapped, and the header's summed totals still carry its
    tokens. Malformed/absent header -> `(None, [], [])`.
    """
    lines: list[dict] = []
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            lines.append(obj)

    if not lines or lines[0].get("type") != "cursor_store_session":
        return None, [], []

    header, rest = lines[0], lines[1:]
    messages = [m for m in rest if "role" in m]
    usage_events: list[dict] = []
    seen_bad_ts_types: set[str] = set()
    for e in rest:
        if e.get("type") != "cursor_usage_event" or "role" in e:
            continue
        ts = e.get("ts")
        if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0:
            usage_events.append(e)
            continue
        # A `ts` that stops being a positive int (e.g. an ISO string) drops
        # this event's per-event billed tokens and cost_cents with nothing
        # to show for it. Dedup by the value's type, not per event.
        ts_type = type(ts).__name__
        if ts_type not in seen_bad_ts_types:
            seen_bad_ts_types.add(ts_type)
            logger.debug(
                "session %s: cursor_usage_event has non-positive-int ts (type=%s); "
                "event's billed tokens/cost dropped",
                trace_id,
                ts_type,
            )
    # Contract says ts-ascending already; don't trust it — mapping relies on order.
    usage_events.sort(key=lambda e: e["ts"])
    return header, usage_events, messages


def _ms_to_dt(ms: object) -> datetime | None:
    """Epoch-ms -> aware UTC datetime, or None if missing/not an int."""
    if not isinstance(ms, int) or isinstance(ms, bool):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _iso_to_dt(value: object) -> datetime | None:
    """ISO-8601 string (e.g. ``"2025-10-02T06:43:45.379Z"``) -> aware UTC
    datetime, or None when missing/unparsable.

    IDE composer bubbles are the only Cursor messages that carry a
    timestamp, and they carry it in this shape; a naive result (no offset)
    is assumed UTC, matching how Cursor writes them.
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


def _as_count(value: object) -> int:
    """Coerce a usage-event token field to a non-negative int (0 on junk)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    return 0


def _assign_events_to_turns(
    events: list[dict],
    turn_open_real_ts: list[datetime | None],
) -> list[int]:
    """Per-event turn index (0-based) for ts-ascending ``events``.

    Timestamp containment when every turn-opening time is known and
    non-decreasing; order-proportional spread otherwise — strategy and
    failure mode documented in the module docstring.

    Args:
        events: usage-event dicts, each with a positive-int ``ts``
            (epoch ms), already sorted ascending.
        turn_open_real_ts: real opening time per turn, aligned with the
            parse's turn order; any ``None`` disables containment.

    Returns:
        A list the length of ``events``; each value in
        ``[0, len(turn_open_real_ts))``.
    """
    k = len(turn_open_real_ts)
    if all(t is not None for t in turn_open_real_ts):
        starts_ms = [int(t.timestamp() * 1000) for t in turn_open_real_ts]  # type: ignore[union-attr]
        if starts_ms == sorted(starts_ms):
            indexes: list[int] = []
            for event in events:
                idx = bisect_right(starts_ms, event["ts"]) - 1
                indexes.append(max(0, idx))
            return indexes
    n = len(events)
    return [min(k - 1, j * k // n) for j in range(n)]


def _stamp_turn_billed_usage(
    turns: list[SpanData],
    turn_open_real_ts: list[datetime | None],
    events: list[dict],
) -> None:
    """Map usage events onto turns and stamp per-turn billed token/cost attrs.

    Mutates the mapped Turn spans in place: ``llm.token_count.prompt``
    (input + cache_read + cache_write, the same prompt convention the
    session header uses), ``.completion``, ``.cache_read``,
    ``.cache_write``, ``.input_fresh`` (codex formula: prompt − cache_read)
    and ``llm.cost.billed_cents`` (float; exact billed cents - preferred
    over a pricing-catalog estimate).
    Turns that received no event keep their zeroed attrs and get NO
    ``llm.cost.billed_cents`` key, so "no billing data" stays
    distinguishable from "billed zero cents".
    """
    per_turn: dict[int, dict[str, float]] = {}
    for event, idx in zip(events, _assign_events_to_turns(events, turn_open_real_ts), strict=True):
        bucket = per_turn.setdefault(
            idx,
            {
                "input": 0,
                "output": 0,
                "cache_read": 0,
                "cache_write": 0,
                "cents": 0.0,
                "has_cents": True,
            },
        )
        bucket["input"] += _as_count(event.get("input_tokens"))
        bucket["output"] += _as_count(event.get("output_tokens"))
        bucket["cache_read"] += _as_count(event.get("cache_read_tokens"))
        bucket["cache_write"] += _as_count(event.get("cache_write_tokens"))
        cents = event.get("charged_cents")
        if cents is None:
            cents = event.get("cost_cents")
        if (
            isinstance(cents, int | float)
            and not isinstance(cents, bool)
            and 0 <= cents < float("inf")
        ):
            bucket["cents"] += float(cents)
        else:
            bucket["has_cents"] = False

    for idx, bucket in per_turn.items():
        turn = turns[idx]
        prompt = int(bucket["input"] + bucket["cache_read"] + bucket["cache_write"])
        turn.attributes["llm.token_count.prompt"] = prompt
        turn.attributes["llm.token_count.completion"] = int(bucket["output"])
        turn.attributes["llm.token_count.cache_read"] = int(bucket["cache_read"])
        turn.attributes["llm.token_count.cache_write"] = int(bucket["cache_write"])
        turn.attributes["llm.token_count.input_fresh"] = max(0, prompt - int(bucket["cache_read"]))
        # total_billed_prompt mirrors prompt: Cursor usage events report the
        # actual billed prompt (not context-window max), so it is the same
        # value. Without this, waste computation falls back to prompt_tokens
        # (which is also correct for Cursor, but the explicit key keeps the
        # `or` fallback working).
        turn.attributes["llm.token_count.total_billed_prompt"] = prompt
        # Contract: absent key = "no billing data" (pricing falls back to
        # the catalog); 0.0 = "genuinely billed zero". Stamping 0.0 for events
        # that carried no cost_cents would kill the catalog fallback.
        if bucket["has_cents"]:
            turn.attributes["llm.cost.billed_cents"] = bucket["cents"]


__all__ = ["parse_cursor_store"]
