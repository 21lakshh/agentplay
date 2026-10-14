"""Parse a Cursor **agent-transcript** JSONL session into the universal SpanData shape.

This is Cursor's third local format, distinct from the two the ``cursor_store``
parser handles (the CLI/agent ``store.db`` NDJSON and the IDE composer ``vscdb``).
Cursor's agent/CLI also writes a lightweight narrative transcript per session at
``~/.cursor/projects/<project-slug>/agent-transcripts/<uuid>/<uuid>.jsonl`` (plus
``subagents/*.jsonl`` children). Each line is an Anthropic-message-shaped record::

    {"role": "user"|"assistant", "message": {"content": [<block>, ...]}}

where each ``<block>`` is ``{"type": "text", "text": ...}`` or
``{"type": "tool_use", "name": ..., "input": {...}, "id": ...}``. Unlike Claude
Code's JSONL this format carries **no** top-level ``type``/``sessionId``, **no**
per-message ``usage`` (billed tokens), and **no** timestamps — so:

* token counts are emitted as 0 (like ``cursor_store`` before its usage-API
  enrichment; there is no local billed-usage source for these files yet), and
* per-turn / per-tool timestamps are **synthesized** monotonically from a single
  base time so the spans keep a stable order and non-zero durations. The absolute
  time is not meaningful (the source has none); ordering, turn/tool
  counts, and the transcript text are.

What it DOES recover : the session
itself, its user/assistant turns, the ``tool_use`` calls (→ ``tool.name`` on TOOL
spans, so ``tool_distribution`` populates), and ``Skill`` / ``activate_skill``
tool calls (→ ``session_signals.skills_used``, so ``skill_distribution`` populates
for Cursor too). It deliberately does not fabricate cost/efficiency numbers the
source can't support.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

from agentplay._core.code_authorship import extract_edit_evidence
from agentplay._core.ingestion.coding_agent._paths import basename as path_basename
from agentplay._core.ingestion.coding_agent._sources._jsonl_utils import (
    _SKILL_TOOL_NAMES,
    parse_jsonl_records,
    source_attrs,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.base import AgentType

# Reuse the Cursor tool-name aliases + file-path arg normalization from the
# sibling store.db parser: both Cursor surfaces emit the same non-canonical
# vocabulary (`StrReplace`/`Shell`/`ReadLints`/`read_file_v2` …), and the
# read/edit/shell/search counters + the shared `file_paths.py` extractor
# only match the canonical Claude/Codex names + a `file_path` key. Without
# this the agent-transcript sessions would count tools but show 0 on the
# read/edit/shell buckets.
from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
    _TOOL_ALIASES,
    _normalize_file_path_arg,
    _skill_name_from_skill_md_path,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData, TraceData

# Synthetic clock: the source has no timestamps, so space turns/tools by a fixed
# step to keep a stable order + non-zero span durations. Absolute time is not
# meaningful for this format (see module docstring).
_STEP = timedelta(seconds=1)


def _text_of(content: object) -> str:
    """Concatenate the ``text`` blocks of a message ``content`` (str or list)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p and isinstance(p, str))


def _tool_uses(content: object) -> list[dict]:
    """The ``tool_use`` blocks of a message ``content`` (empty if none)."""
    if not isinstance(content, list):
        return []
    return [
        block for block in content if isinstance(block, dict) and block.get("type") == "tool_use"
    ]


def parse_cursor_agent_transcript(
    data: bytes,
    trace_id: str,
    user_id: str | None = None,
    project_name: str | None = None,
) -> tuple[TraceData, list[SpanData]]:
    """Parse a Cursor agent-transcript JSONL file into a span tree.

    Args:
        data: raw JSONL bytes.
        trace_id: the source-prefixed session id (``local:cursor:<uuid>``).
        user_id: identifier to stash on the root span's ``user.id``.
        project_name: project slug resolved by ``LocalSource`` (from the path).

    Returns:
        ``(trace, all_spans)`` — a root Session (AGENT) span, one CHAIN span per
        turn, and one TOOL span per ``tool_use``. Empty span list for a file with
        no parseable turns (the caller skips it rather than storing an empty
        session).
    """
    # every record carries the shared source anchors (offset + hash).
    records = [obj for obj in parse_jsonl_records(data) if obj.get("role") in ("user", "assistant")]
    # Ordering only: this format records no activity timestamps. Never re-date
    # identical bytes to the time this parser happens to run.
    base = datetime(1970, 1, 1, tzinfo=UTC)
    if not records:
        return TraceData(trace_id=trace_id, start_time=base, end_time=base), []

    root_id = f"{trace_id}:root"
    all_spans: list[SpanData] = []

    # Group records into turns: a turn opens on a user message and absorbs the
    # assistant messages that follow it (until the next user message). Leading
    # assistant messages (no preceding user) attach to an implicit turn 0.
    turns: list[dict] = []
    current: dict | None = None
    for rec in records:
        if rec.get("role") == "user":
            current = {"user": rec, "assistants": []}
            turns.append(current)
        else:
            if current is None:
                current = {"user": None, "assistants": []}
                turns.append(current)
            current["assistants"].append(rec)

    # Collect skill invocations across the whole session (Skill/activate_skill
    # tool calls) → session_signals.skills_used, so skill_distribution populates.
    skills_used: list[dict] = []
    clock = base

    for turn_num, turn in enumerate(turns, 1):
        turn_id = f"{trace_id}:turn:{turn_num}"
        turn_start = clock
        user_msg = turn["user"].get("message", {}) if turn["user"] else {}
        user_query = _text_of(user_msg.get("content")) if isinstance(user_msg, dict) else ""
        assistant_text_parts: list[str] = []

        # Advance the synthetic clock once per assistant message so tools/turns
        # keep a stable monotonic order.
        tool_spans_this_turn: list[SpanData] = []
        for aline in turn["assistants"]:
            msg = aline.get("message", {})
            if not isinstance(msg, dict):
                continue
            assistant_text_parts.append(_text_of(msg.get("content")))
            for block in _tool_uses(msg.get("content")):
                clock += _STEP
                raw_name = block.get("name", "unknown") or "unknown"
                # A malformed tool block can carry a non-string (unhashable) name
                # — e.g. a list/dict — which would make `_TOOL_ALIASES.get(name)`
                # raise TypeError and, since get_spans_by_session doesn't catch
                # parser errors, drop the WHOLE session. Coerce to str first.
                if not isinstance(raw_name, str):
                    raw_name = str(raw_name)
                tool_input = block.get("input", {})
                tool_use_id = block.get("id", "") or f"{turn_num}:{len(tool_spans_this_turn)}"
                # Skill detection uses the RAW name (skill/activate_skill aren't
                # aliased); the span uses the canonical alias so the read/edit/
                # shell/search counters match.
                if str(raw_name).strip().lower() in _SKILL_TOOL_NAMES:
                    skill_arg = ""
                    if isinstance(tool_input, dict):
                        skill_arg = (
                            tool_input.get("skill")
                            or tool_input.get("name")
                            or tool_input.get("command")
                            or ""
                        )
                    if skill_arg:
                        skills_used.append({"path": str(skill_arg), "count": 1})
                tool_name = _TOOL_ALIASES.get(raw_name, raw_name)
                if isinstance(tool_input, dict):
                    tool_input = _normalize_file_path_arg(tool_input)
                    # the _SKILL_TOOL_NAMES check above only ever
                    # matches Claude Code's own tool vocabulary ("skill"/
                    # "activate_skill") — genuine Cursor skill usage instead
                    # shows up as a plain Read call on the skill's SKILL.md
                    # path (confirmed against real local Cursor IDE history;
                    # see cursor_store.py's _skill_read_from_tool for the
                    # full rationale). Match on basename, not a fixed root.
                    if tool_name == "Read":
                        skill_path = tool_input.get("file_path")
                        if (
                            isinstance(skill_path, str)
                            and skill_path
                            and path_basename(skill_path).lower() == "skill.md"
                        ):
                            canonical = _skill_name_from_skill_md_path(skill_path)
                            if canonical:
                                skills_used.append({"path": canonical, "count": 1})
                    # Serialize file_path FIRST so it survives the input.value
                    # truncation below: a large StrReplace / multi-path arg would
                    # otherwise push the appended key past the 2000-char cap, and
                    # the shared file_paths.py extractor (which keys on file_path)
                    # would find nothing — losing edit/read file attribution.
                    if "file_path" in tool_input:
                        tool_input = {
                            "file_path": tool_input["file_path"],
                            **{k: v for k, v in tool_input.items() if k != "file_path"},
                        }
                input_str = (
                    json.dumps(tool_input) if isinstance(tool_input, dict) else str(tool_input)
                )
                tool_spans_this_turn.append(
                    SpanData(
                        span_id=f"{trace_id}:tool:{tool_use_id}",
                        trace_id=trace_id,
                        parent_id=turn_id,
                        name=str(tool_name),
                        span_kind="CHAIN",
                        status_code="OK",
                        start_time=clock,
                        end_time=clock,
                        attributes={
                            "tool.name": str(tool_name),
                            "input.value": input_str[:2000],
                            # Narrative calls have neither completion evidence nor
                            # observed clocks. The legacy OK span is not a verdict.
                            "code.edit_evidence": extract_edit_evidence(
                                str(tool_name), tool_input, successful=False
                            ),
                            "output.value": "",
                            **source_attrs(aline),
                        },
                    )
                )
        clock += _STEP
        turn_end = clock
        assistant_text = "\n".join(p for p in assistant_text_parts if p)

        all_spans.append(
            SpanData(
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
                    "cwd": "",
                    # No billed usage in this format — emit explicit zeros so the
                    # aggregator treats them as "known 0", not missing.
                    "llm.token_count.prompt": 0,
                    "llm.token_count.completion": 0,
                    "llm.token_count.cache_read": 0,
                    "llm.token_count.cache_write": 0,
                    "llm.model_name": "",
                    # anchored to the record that opened the turn.
                    **source_attrs(
                        turn["user"] or (turn["assistants"][0] if turn["assistants"] else None)
                    ),
                },
            )
        )
        all_spans.extend(tool_spans_this_turn)

    session_start = base
    session_end = clock

    # No "interrupts": {"total": 0} placeholder — `build_sidecar_signals` treats
    # any non-empty dict as a real signal, so a hardcoded zero would make every
    # signal-less session look like it has interrupt data and create a sidecar
    # for nothing (same reasoning as cursor_store.py). This format
    # has no interrupt signal source at all, so omitting the key is what "no
    # signal source" actually means.
    signals = {"slash_commands": [], "skills_used": skills_used}
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
            # session.id is required on every root span by the TraceSource
            # contract (all sibling parsers set it to trace_id).
            "session.id": trace_id,
            "harness.agent": str(AgentType.CURSOR),
            "harness.session_signals": json.dumps(signals),
            "user.id": user_id or "",
            "project.name": project_name or "",
            "llm.model_name": "",
        },
    )
    all_spans.insert(0, root_span)

    trace = TraceData(
        trace_id=trace_id,
        start_time=session_start,
        end_time=session_end,
        spans=all_spans,
    )
    for span in all_spans:
        span.attributes["session.timestamps_observed"] = False
    return trace, all_spans
