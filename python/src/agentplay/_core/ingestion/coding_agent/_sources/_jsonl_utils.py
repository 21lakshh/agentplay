"""Shared parsing helpers for Claude Code JSONL transcripts.

The local ~/.claude file adapter and any other consumer of the Claude
Code JSONL format share these helpers: noise filtering, turn grouping,
session-signal extraction (slash commands / skill loads / interrupts).

The format is defined by Claude Code itself - keeping the helpers in one
place means a Claude Code format bump is a single edit.
"""

from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, UTC
import hashlib
import json
import logging
import re

from agentplay._core.ingestion.coding_agent.skill_names import normalize_skill_name

logger = logging.getLogger(__name__)

# Line types that are housekeeping, not conversation.
SKIP_TYPES = {"progress", "system", "file-history-snapshot", "queue-operation", "summary"}

# Regex helpers for signal extraction.
_RE_COMMAND_NAME = re.compile(r"<command-name>([^<]+)</command-name>", re.IGNORECASE)
_RE_COMMAND_MESSAGE = re.compile(r"<command-message>([^<]+)</command-message>", re.IGNORECASE)
_RE_SKILL_DIR = re.compile(r"base directory for this skill:\s*(\S+)", re.IGNORECASE)
# Modern skill-invocation tool names (assistant `tool_use` blocks). Current
# Claude Code invokes a skill via the `Skill` tool (input `{"skill": "<name>"}`),
# NOT the legacy "base directory for this skill:" user-message injection matched
# by `_RE_SKILL_DIR` above — so on current versions skills went entirely
# uncounted. Lower-cased for case-insensitive matching.
_SKILL_TOOL_NAMES = {"skill", "activate_skill"}
# Bound on per-run slash-command entries a session reports.
_MAX_SLASH_INVOCATIONS = 200


def parse_dt(value: str) -> datetime:
    """Parse an ISO-8601 timestamp (with `Z` shorthand) into an AWARE datetime.

    An offset-less value is read as UTC: every caller compares or maxes these
    against `Z`/offset timestamps from the same transcript, and Python refuses
    a naive/aware comparison — one bare timestamp would otherwise turn a
    whole session's capture into a TypeError.
    """
    value = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def safe_parse_dt(value: object) -> datetime | None:
    """:func:`parse_dt`, but never raises: None for a non-string or malformed value."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_dt(value)
    except ValueError:
        return None


def drop_untyped(container: dict, keys: tuple[str, ...], kind: type | tuple) -> None:
    """Delete the ``keys`` whose value is not a ``kind``, so ``.get`` defaults apply.

    ``.get(key, default)`` only covers an ABSENT key; a retyped value (``null``, a
    number where an id string belongs) would raise deep inside a parser. Removing
    it at the boundary makes it read as absent instead.
    """
    for key in keys:
        if key in container and not isinstance(container[key], kind):
            del container[key]


def as_count(value: object) -> int:
    """``value`` as a token count: 0 when absent, null, negative, or not numeric."""
    if isinstance(value, bool):
        return 0
    try:
        return max(int(value), 0)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        return 0


def as_dict(value: object) -> dict:
    """``value`` when it is a dict, else ``{}`` -- for fields a newer schema may retype."""
    return value if isinstance(value, dict) else {}


def extract_user_content(message: dict) -> str:
    """Pull the text out of a user message — handles str / list-of-blocks."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
            elif isinstance(block, str):
                texts.append(block)
        return "\n".join(texts)
    return str(content)


def extract_tool_results(message: dict) -> dict[str, dict]:
    """Pull `tool_use_id -> tool_result_block` from a user message."""
    if not isinstance(message, dict):
        return {}
    content = message.get("content", [])
    if not isinstance(content, list):
        return {}
    results = {}
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            tool_use_id = block.get("tool_use_id", "")
            if tool_use_id:
                results[tool_use_id] = block
    return results


# Reserved key stamped onto every parsed line dict by `parse_lines`: the byte
# offset of the line's START within the normalized stream. Leading
# underscore keeps it visually distinct from real transcript keys; nothing in
# any known agent transcript format uses this name. Consumers that
# hash or re-serialize a line dict must exclude it.
SOURCE_OFFSET_KEY = "_ent_offset"
# A content hash of the raw record, stamped beside the offset so every
# parser's spans carry the same anchor block.
SOURCE_HASH_KEY = "_ent_hash"
# The addressable row/document key of a record from a REWRITTEN source (a
# vscdb composer bubble id, an opencode message id) - the resume anchor where
# a byte offset cannot be one. Stamped by the producer that knows the key
# (the Cursor synthesizer, opencode's own record id), carried as
# `source.row_cursor`, and - like every stamp key - excluded from the content
# hash, so adding it never re-identifies a turn already stored under its hash.
SOURCE_ROW_CURSOR_KEY = "_ent_row_cursor"
_STAMP_KEYS = frozenset({SOURCE_OFFSET_KEY, SOURCE_HASH_KEY, SOURCE_ROW_CURSOR_KEY})

SOURCE_KIND_JSONL = "jsonl"
SOURCE_KIND_SQLITE = "sqlite"
SOURCE_KIND_JSON_DOC = "json_doc"


def record_hash(record: object) -> str | None:
    """Content-anchored identity of one raw record.

    16 hex chars of sha1 over the canonical JSON of the record with the
    parser's own stamps removed — identical regardless of where a rewrite
    placed the line. None when the record cannot be serialised.
    """
    if not isinstance(record, dict):
        return None
    try:
        body = {k: v for k, v in record.items() if k not in _STAMP_KEYS}
        canonical = json.dumps(body, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def _has_usable_type(obj: object) -> bool:
    """A decoded line is a record only when it is a dict whose ``type`` (if any) is a string.

    Every parser keys on ``type`` (set membership, dict dispatch); a list or dict
    there is unhashable and would abort the whole session.
    """
    return isinstance(obj, dict) and isinstance(obj.get("type", ""), str)


def _drop_bad_timestamp(obj: dict) -> None:
    """Remove a non-ISO ``timestamp`` so downstream time math treats it as absent.

    The rest of the record is still readable, so it is kept rather than skipped.
    """
    if "timestamp" in obj and safe_parse_dt(obj["timestamp"]) is None:
        del obj["timestamp"]


def parse_jsonl_records(data: bytes) -> list[dict]:
    """Every dict line of a JSONL document, each stamped with its source anchors.

    The generic sibling of :func:`parse_lines` (which additionally applies
    claude-code's noise filter): no type is skipped, every kept dict carries
    :data:`SOURCE_OFFSET_KEY` (byte offset of its line's start in the
    normalized stream, stable across re-parses of an append-only file) and
    :data:`SOURCE_HASH_KEY` (see :func:`record_hash`). Used by the parsers of
    every append-only JSONL agent (codex, kiro, opencode, cursor-agent
    transcripts) so all of them carry the same anchor block.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace").encode("utf-8")
    records: list[dict] = []
    offset = 0
    skipped = 0
    for raw_line in data.split(b"\n"):
        line_start = offset
        offset += len(raw_line) + 1
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            skipped += 1
            continue
        if not _has_usable_type(obj):
            skipped += 1
            continue
        obj[SOURCE_HASH_KEY] = record_hash(obj)
        obj[SOURCE_OFFSET_KEY] = line_start
        _drop_bad_timestamp(obj)
        records.append(obj)
    if skipped:
        logger.debug("skipped %d unreadable JSONL line(s)", skipped)
    return records


def source_attrs(
    record: object, *, kind: str = SOURCE_KIND_JSONL, row_cursor: str | None = None
) -> dict:
    """The span attributes of the source-cursor block for one originating record.

    ``source.kind`` always; ``source.byte_offset`` and ``source.content_hash``
    when the record carries the stamps (:func:`parse_jsonl_records`), the
    hash computed on the fly for an unstamped record so a parser that builds
    its own records still anchors them; ``source.row_cursor`` when the caller
    passes one or the record carries :data:`SOURCE_ROW_CURSOR_KEY`
    (rewritten sources whose records have an addressable key).
    Pure.
    """
    attrs: dict = {"source.kind": kind}
    if isinstance(row_cursor, str) and row_cursor:
        attrs["source.row_cursor"] = row_cursor
    if not isinstance(record, dict):
        return attrs
    offset = record.get(SOURCE_OFFSET_KEY)
    if offset is not None:
        attrs["source.byte_offset"] = offset
    content_hash = record.get(SOURCE_HASH_KEY) or record_hash(record)
    if content_hash is not None:
        attrs["source.content_hash"] = content_hash
    stamped = record.get(SOURCE_ROW_CURSOR_KEY)
    if "source.row_cursor" not in attrs and isinstance(stamped, str) and stamped:
        attrs["source.row_cursor"] = stamped
    return attrs


def parse_lines(data: bytes) -> list[dict]:
    """Parse JSONL bytes into dicts, skipping non-conversation lines.

    Each kept dict is stamped with :data:`SOURCE_OFFSET_KEY` — the byte
    offset of its line's start (a transcript-anchored identity, stable
    across re-parses of an append-only file because earlier bytes never move). Offsets are measured over the
    NORMALIZED stream (post `errors="replace"` re-encode below), not the
    raw file bytes — the two differ only when the file held invalid UTF-8,
    and stability across re-parses (which all run the same normalization)
    is the property that matters, not agreement with `stat().st_size`.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace").encode("utf-8")
    lines = []
    offset = 0
    skipped = 0
    for raw_line in data.split(b"\n"):
        line_start = offset
        offset += len(raw_line) + 1  # +1 for the split-consumed "\n"
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            obj = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError):
            skipped += 1
            continue
        if not _has_usable_type(obj):
            skipped += 1
            continue
        line_type = obj.get("type", "")
        if line_type in SKIP_TYPES:
            continue
        if "snapshot" in obj and "messageId" in obj:
            continue
        obj[SOURCE_HASH_KEY] = record_hash(obj)
        obj[SOURCE_OFFSET_KEY] = line_start
        _drop_bad_timestamp(obj)
        lines.append(obj)
    if skipped:
        logger.debug("skipped %d unreadable JSONL line(s)", skipped)
    return lines


def _is_image_source_line(msg: dict) -> bool:
    """Image-source reference lines aren't real user turns."""
    text = extract_user_content(msg)
    return text.startswith("[Image source:") or text.startswith("[Image: source:")


def _is_meta_message(msg: dict) -> bool:
    """Claude Code's own system markers + slash-command wrappers.

    Signals from these lines (slash-command counts, skill loads,
    interrupt counts) should be extracted via `extract_session_signals`
    BEFORE this filter runs — otherwise the data is gone.
    """
    text = extract_user_content(msg).strip()
    if not text:
        return True
    head = text[:200]
    if head.startswith("[Request interrupted by user"):
        return True
    if head.startswith("<task-notification>"):
        return True
    if head.startswith("<local-command-caveat>") or head.startswith("<local-command-stdout>"):
        return True
    if "being continued from a previous conversation" in head:
        return True
    if head.lower().startswith("base directory for this skill:"):
        return True
    if head.startswith("<command-name>/compact"):
        return True
    if head.startswith("<command-name>") or head.startswith("<command-message>"):
        stripped = _RE_COMMAND_NAME.sub("", text)
        stripped = _RE_COMMAND_MESSAGE.sub("", stripped)
        stripped = re.sub(r"<command-args>[^<]*</command-args>", "", stripped)
        stripped = stripped.strip()
        if not stripped or len(stripped) < 30:
            return True
    return False


# Cap a captured skill body so a pathological transcript (a skill that inlines a
# huge file) can't bloat the definition store. Sized so large
# but legitimate skill bodies stay intact while a runaway inline is bounded.
_SKILL_BODY_MAX_CHARS = 65_536


def _skill_body_after_marker(text: str) -> str:
    """Return the SKILL.md body that follows the base-directory marker line.

    A skill load injects the marker line (``base directory for this skill:
    <dir>``) followed by the skill's SKILL.md content in the same message. This
    drops the marker line and returns the remaining body, stripped and
    size-capped (:data:`_SKILL_BODY_MAX_CHARS`).

    Args:
        text: the full injected skill-load message (marker line first).

    Returns:
        The body after the marker line, or ``""`` when there's nothing after it.
    """
    _marker, _, rest = text.lstrip().partition("\n")
    return rest.strip()[:_SKILL_BODY_MAX_CHARS]


def _skill_tool_invocations(message: object) -> list[tuple[str, str | None]]:
    """`(skill name, tool_use id)` for each assistant `Skill`/`activate_skill` call.

    The modern Claude Code skill-invocation shape: an assistant `tool_use` block
    whose `name` is in `_SKILL_TOOL_NAMES` and whose `input` carries the skill
    under `"skill"` (or `"command"`). The `tool_use` id is returned alongside so
    the caller can dedupe blocks that a compacted session re-serialized — the
    same stable id reappears across replayed turns, and `extract_session_signals`
    runs on RAW lines, before any replay-dedup pass. Empty list for any
    non-conforming message (bare string content, no tool blocks) — never raises.
    """
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    out: list[tuple[str, str | None]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if str(block.get("name", "")).strip().lower() not in _SKILL_TOOL_NAMES:
            continue
        inp = block.get("input")
        if isinstance(inp, dict):
            name = inp.get("skill") or inp.get("command")
            if isinstance(name, str) and name.strip():
                tool_id = block.get("id")
                out.append((name.strip(), tool_id if isinstance(tool_id, str) else None))
    return out


def _typed_command_for_load(canonical: str, typed_unpaired: Counter[str]) -> str | None:
    """The unpaired typed command a Skill load belongs to, or None.

    A namespaced load pairs only with the same `plugin:command`, so `xlsx:docs`
    never pairs with a typed `myplugin:docs`. A bare load (older harnesses name
    only the leaf) pairs with a typed command of that leaf when exactly one
    plugin's command with that leaf is waiting; two candidates are ambiguous
    and the load counts on its own.

    Args:
        canonical: The load's normalized skill name.
        typed_unpaired: Unpaired typed commands of this turn, by full name.

    Returns:
        The typed command's full name, or None.
    """
    if typed_unpaired.get(canonical, 0) > 0:
        return canonical
    if ":" in canonical:
        return None
    same_leaf = [n for n, c in typed_unpaired.items() if c > 0 and n.rsplit(":", 1)[-1] == canonical]
    return same_leaf[0] if len(same_leaf) == 1 else None


def extract_session_signals(
    lines: list[dict], seen_skill_tool_ids: set[str] | None = None
) -> dict:
    """Pull structured signals from raw JSONL lines BEFORE noise filtering.

    Returns a dict with:
      slash_commands: [{"name": str, "count": int}], one count per run
      slash_invocations: [{"name": str, "record_uuid": str|None, "ts": str|None}]
      skills_used:    [{"path": str, "count": int}], one per skill activation
      interrupts:     {"total": int, "during_tool_use": int}

    These are stashed on the session payload for later aggregation. This
    set is the deterministic, Claude-Code-only subset of session signals.

    Args:
        seen_skill_tool_ids: Shared, mutated in place, when a caller runs
            this over a session's `main_lines` and `sidechain_lines`
            separately (`parse_claude_code`): a
            subagent's own Skill call is echoed verbatim into BOTH the
            parent transcript (`isSidechain: false`) and its own file
            (`isSidechain: true`), sharing one tool_use id. Two independent
            fresh sets would each count it once, and `fold_subagent_skills`
            would then double it. None (the default) creates a fresh,
            call-local set.
    """
    slash_counts: Counter[str] = Counter()
    # Keyed by CANONICAL skill name (``normalize_skill_name``), never the raw
    # path/name. A single skill invocation surfaces under two shapes — the
    # assistant ``Skill`` tool_use (raw ``"my-skill"``) and the injected "base
    # directory for this skill: …/skills/my-skill" marker (raw ``"skills/my-skill"``)
    # — which normalize to the same name; keying on the canonical name collapses
    # them into one entry instead of two half-counts.
    skill_counts: Counter[str] = Counter()
    # canonical skill name -> its SKILL.md body (the longest seen this session).
    # Claude Code injects the full skill content in the same synthetic message as
    # the base-directory marker, so we capture it here rather than throwing it
    # away - it's what surfaces the skill's actual definition, not just a name.
    skill_bodies: dict[str, str] = {}
    # tool_use ids of Skill calls already counted — a compacted session replays
    # prior assistant turns verbatim, so the same block reappears in raw lines.
    if seen_skill_tool_ids is None:
        seen_skill_tool_ids = set()
    # canonical names already counted via a ``Skill`` tool_use this session. The
    # base-directory marker is emitted as a CONSEQUENCE of that same tool call, so
    # it must not re-count skills already seen here (that would double-count
    # each use); it still counts skills seen ONLY via the marker (legacy sessions).
    tool_counted_skills: set[str] = set()
    # A plugin skill's tool call names it `<plugin>:<skill>`, but its marker path
    # only yields `<skill>` (the plugin's folder name need not match the
    # namespace) — and two skills (two plugins, or a plugin skill and a plain
    # one) can share that same leaf. Two failure modes a naive mapping hits:
    #   - a single bare-name -> namespace VALUE let a later invocation's marker
    #     resolve to an earlier, unrelated skill's namespace once their leaves
    #     collided (a queue fixes this — FIFO, one entry per invocation);
    #   - overwriting that value with only the LATEST invocation instead
    #     mishandled one assistant message with TWO same-leaf calls before
    #     either marker arrives: the first call's entry was discarded before
    #     its own marker ever got a chance to consume it.
    # A queue alone reintroduces the first failure mode once some invocation's marker
    # is simply missing (a truncated transcript): its entry sits in the queue
    # forever, so a later, unrelated same-leaf call's marker pops it instead
    # of its own. What distinguishes "missing marker, safe to discard" from
    # "same-message batch, must preserve order" is exactly whether the calls
    # share a CONVERSATION turn — not merely a raw line — so each leaf's
    # queue is reset (discarding whatever a PRIOR, now-closed turn left
    # unconsumed) the first time a new turn touches it, and only appended to
    # for the rest of that same turn. A raw line index is the wrong identity
    # for this: Claude Code can emit several SEPARATE
    # assistant records within one logical turn, with a plain tool-result
    # continuation line in between (a different tool's output, not this
    # skill's own marker) — counting that as a turn boundary would reset a
    # still-pending, still-unconsumed entry that belongs later in the SAME
    # turn. Track turn identity the same way group_into_turns does: bump it
    # only on a genuine new user message, never on a tool-result/image/meta
    # continuation.
    pending_by_bare: dict[str, deque[str]] = {}
    pending_turn_by_bare: dict[str, int] = {}
    interrupts_total = 0
    interrupts_during_tool = 0
    turn_index = 0
    # typed plugin commands of the CURRENT turn whose Skill load has
    # not been seen yet, by full `plugin:command` name. The load a typed command
    # triggers is the same use, so it pairs with the command instead of
    # counting again. Reset whenever a new turn types a command.
    typed_unpaired: Counter[str] = Counter()
    typed_turn = -1
    # message uuids of typed-command records already counted. Claude
    # Code re-writes a command record verbatim (same uuid) on resume/compaction.
    seen_command_uuids: set[str] = set()
    # one entry per typed run (metadata only: name, record uuid,
    # timestamp) so a consumer can drop a resume/fork copy that landed in
    # another session file with the same record uuid.
    slash_invocations: list[dict] = []

    for line in lines:
        line_type = line.get("type")
        # Modern skill invocation lives on the ASSISTANT side: a `Skill`/
        # `activate_skill` tool call. The legacy "base directory for this skill:"
        # marker below only ever appeared in synthetic USER messages on pre-
        # Skill-tool Claude Code, so current sessions had their skills go
        # entirely uncounted (empty skill_distribution). Count both shapes.
        if line_type == "assistant":
            for skill, tool_id in _skill_tool_invocations(line.get("message", {})):
                # Count each distinct tool_use block once, even when a compacted
                # session re-serializes it across replayed turns.
                is_replay = tool_id is not None and tool_id in seen_skill_tool_ids
                if tool_id is not None:
                    seen_skill_tool_ids.add(tool_id)
                canonical = normalize_skill_name(skill)
                if canonical is None:
                    continue
                # Queue the resolution even on a replay:
                # a compacted session replays a marker along with its
                # triggering tool_use, and skipping the queue entry here
                # left that replayed marker with nothing to consume,
                # falling back to the bare name and splitting one skill's
                # count/body across two keys.
                bare = canonical.rsplit(":", 1)[1] if ":" in canonical else canonical
                if pending_turn_by_bare.get(bare) != turn_index:
                    pending_by_bare[bare] = deque()
                    pending_turn_by_bare[bare] = turn_index
                pending_by_bare[bare].append(canonical)
                # Marked whether or not this occurrence is a replay: a caller
                # sharing `seen_skill_tool_ids` across a parent/sidechain pair
                # means THIS call's own first sight
                # of the tool_id can already be `is_replay` (claimed by the
                # other call) -- its own marker's tool-uncounted fallback below
                # must still recognize the skill as tool-counted, or it
                # double-counts via that path instead. Only the COUNT itself
                # stays gated on `is_replay`.
                tool_counted_skills.add(canonical)
                if is_replay:
                    continue
                paired = _typed_command_for_load(canonical, typed_unpaired) if typed_turn == turn_index else None
                if paired is not None:
                    # The load a typed `/<plugin>:<command>` triggered in this
                    # turn: that command already counted this use.
                    typed_unpaired[paired] -= 1
                else:
                    skill_counts[canonical] += 1
            continue
        if line_type != "user":
            continue
        msg = line.get("message", {})
        # Same predicate group_into_turns uses to decide "still this turn":
        # a tool-result reply, an image-source line, or a meta message never
        # opens a new one.
        if (
            not extract_tool_results(msg)
            and not _is_image_source_line(msg)
            and not _is_meta_message(msg)
        ):
            turn_index += 1
        text = extract_user_content(msg)
        if not text:
            continue

        if text.startswith("[Request interrupted by user"):
            interrupts_total += 1
            if "for tool use" in text[:60]:
                interrupts_during_tool += 1
            continue

        # One slash-command run writes BOTH tags into one message with the same
        # name (`<command-name>/x</command-name>`, `<command-message>x
        # </command-message>`). Counting each tag doubled every command on every
        # CLI, so each distinct name counts once per message.
        # `/compact` keeps its historical single count.
        message_commands = {
            name
            for regex in (_RE_COMMAND_NAME, _RE_COMMAND_MESSAGE)
            for match in regex.finditer(text)
            if (name := match.group(1).strip().lstrip("/"))
        }
        if message_commands:
            # Resume and compaction re-write a command record verbatim, same
            # uuid; the copy is not a new run.
            record_uuid = line.get("uuid")
            if isinstance(record_uuid, str) and record_uuid:
                if record_uuid in seen_command_uuids:
                    message_commands = set()
                seen_command_uuids.add(record_uuid)
        for name in sorted(message_commands):
            slash_counts[name] += 1
            if len(slash_invocations) < _MAX_SLASH_INVOCATIONS:
                record_uuid = line.get("uuid")
                ts = line.get("timestamp")
                slash_invocations.append(
                    {
                        "name": name[:128],
                        "record_uuid": record_uuid[:64] if isinstance(record_uuid, str) else None,
                        "ts": ts[:40] if isinstance(ts, str) else None,
                    }
                )

        # A plugin command run as `/<plugin>:<command>` is a skill use too:
        # Claude Code writes it as a slash command, and the Skill
        # load it may trigger is the same use, so that load pairs
        # with the command instead of counting again. Queued like a Skill call
        # so a base-directory marker for the same command in this turn is not
        # counted again either.
        plugin_commands = {
            canonical
            for name in message_commands
            if ":" in name and (canonical := normalize_skill_name(name)) is not None
        }
        for canonical in plugin_commands:
            bare = canonical.rsplit(":", 1)[1]
            if pending_turn_by_bare.get(bare) != turn_index:
                pending_by_bare[bare] = deque()
                pending_turn_by_bare[bare] = turn_index
            pending_by_bare[bare].append(canonical)
            if typed_turn != turn_index:
                typed_unpaired = Counter()
                typed_turn = turn_index
            typed_unpaired[canonical] += 1
            tool_counted_skills.add(canonical)
            skill_counts[canonical] += 1

        # Skill loads: Claude Code injects "base directory for this skill: <dir>"
        # at the START of a synthetic user message. Match only when the message
        # begins with the phrase — the phrase quoted mid-text (in code, docs, or
        # a nested transcript, e.g. a Rust `Regex::new(r"base directory for this
        # skill:\s*(\S+)").unwrap()`) is NOT a real skill load and would leak
        # regex fragments in as garbage skill names. Validate the captured path
        # too, so a phrase-prefixed-but-junk token can't slip through either.
        stripped = text.lstrip()
        if stripped[:200].lower().startswith("base directory for this skill:"):
            match = _RE_SKILL_DIR.match(stripped)
            if match:
                path = match.group(1).strip().rstrip("/")
                short = path.split("/.claude/", 1)[-1] if "/.claude/" in path else path
                canonical = normalize_skill_name(short)
                if canonical is not None:
                    # Only a pending call from THIS turn can own the marker: a
                    # marker is a meta message, so it never opens a turn and
                    # always shares its own tool call's turn. An entry left by
                    # an earlier turn (its own marker never arrived) is stale;
                    # consuming it would file a marker-only invocation under an
                    # unrelated call's namespace and drop its count.
                    if pending_turn_by_bare.get(canonical) != turn_index:
                        pending_by_bare.pop(canonical, None)
                    pending = pending_by_bare.get(canonical)
                    if pending:
                        canonical = pending.popleft()
                        # A typed command whose body the harness injected
                        # directly has now loaded; a later Skill call in this
                        # turn is a separate activation, not its load.
                        if typed_turn == turn_index and typed_unpaired.get(canonical, 0) > 0:
                            typed_unpaired[canonical] -= 1
                    # This marker is injected as a CONSEQUENCE of a `Skill` tool
                    # call already counted above (same invocation) — counting it
                    # again double-counts. Increment only for skills the
                    # tool path never saw (legacy pre-Skill-tool sessions, where the
                    # marker is the sole signal); capture the body either way.
                    if canonical not in tool_counted_skills:
                        skill_counts[canonical] += 1
                    # The skill's SKILL.md body is the rest of this injected
                    # message (everything after the base-directory marker line).
                    # Keep the longest we've seen — a later re-load can be a
                    # truncated re-inject, and the fullest copy is the useful one.
                    body = _skill_body_after_marker(text)
                    if body and len(body) > len(skill_bodies.get(canonical, "")):
                        skill_bodies[canonical] = body

    return {
        "slash_commands": [{"name": n, "count": c} for n, c in slash_counts.most_common()],
        "slash_invocations": slash_invocations,
        # ``path`` carries the canonical skill name (already normalized), which
        # eliminates the raw ``skills/<name>`` vs ``<name>`` split.
        "skills_used": [{"path": p, "count": c} for p, c in skill_counts.most_common()],
        # One captured definition per skill loaded this session (name + body).
        "skill_definitions": [
            {"name": p, "body": skill_bodies[p]}
            for p, _ in skill_counts.most_common()
            if skill_bodies.get(p)
        ],
        "interrupts": {"total": interrupts_total, "during_tool_use": interrupts_during_tool},
    }


def fold_subagent_skills(signals: dict, subagent_signals: dict) -> dict:
    """Add a subagent transcript's skill signals into the parent's.

    Claude Code callers run :func:`extract_session_signals` separately over a
    session's own lines and its spawned subagents' (``isSidechain``) lines,
    because ``slash_commands`` and ``interrupts`` describe what the DEVELOPER
    typed — a subagent's synthetic "user" turn is the parent's own tool call,
    not a developer action, so those two stay parent-only. A subagent
    invoking a skill is different: it is still work the developer's own
    dispatch caused, and excluding it is what made a skill like a plugin's
    orchestrator-dispatched command invisible to the "skills used" counts.
    Only ``skills_used`` and
    ``skill_definitions`` fold in here; the other two keys pass through from
    ``signals`` unchanged.

    Safe to call with ``subagent_signals`` from an entirely separate
    :func:`extract_session_signals` call: a subagent's own lines carry no
    duplicate copy in the parent's lines (sessions whose only skill
    invocations were subagent-scoped carried zero parent skill entries), so
    this cannot double an invocation the parent-only call already counted.

    Args:
        signals: The parent-only :func:`extract_session_signals` result.
        subagent_signals: The same function's result over the session's
            ``isSidechain`` lines.

    Returns:
        A new dict, ``signals`` with ``skills_used``/``skill_definitions``
        folded; ``signals`` and ``subagent_signals`` are not mutated.
    """
    counts: dict[str, int] = {e["path"]: e["count"] for e in signals["skills_used"]}
    for e in subagent_signals["skills_used"]:
        counts[e["path"]] = counts.get(e["path"], 0) + e["count"]
    # Longest body wins per name, matching the same rule
    # `extract_session_signals` already applies within one call.
    bodies: dict[str, str] = {d["name"]: d["body"] for d in signals["skill_definitions"]}
    for d in subagent_signals["skill_definitions"]:
        if len(d["body"]) > len(bodies.get(d["name"], "")):
            bodies[d["name"]] = d["body"]
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        **signals,
        "skills_used": [{"path": p, "count": c} for p, c in ranked],
        "skill_definitions": [{"name": p, "body": bodies[p]} for p, _ in ranked if bodies.get(p)],
    }


def group_into_turns(lines: list[dict]) -> list[dict]:
    """Group conversation lines into turns (user → assistant exchanges).

    Each turn dict has:
      user                       — the user message line (or None)
      assistants                 — list of assistant message lines
      tool_results               — {tool_use_id: tool_result_block}
      tool_result_timestamps     — {tool_use_id: timestamp}
    """
    turns: list[dict] = []
    current_turn: dict | None = None

    for line in lines:
        line_type = line.get("type", "")

        if line_type == "user":
            msg = line.get("message", {})
            tool_results = extract_tool_results(msg)

            if tool_results and current_turn:
                current_turn["tool_results"].update(tool_results)
                ts = line.get("timestamp", "")
                for tid in tool_results:
                    current_turn["tool_result_timestamps"][tid] = ts
            elif _is_image_source_line(msg) and current_turn:
                pass
            elif _is_meta_message(msg):
                pass
            else:
                if current_turn:
                    turns.append(current_turn)
                current_turn = {
                    "user": line,
                    "assistants": [],
                    "tool_results": {},
                    "tool_result_timestamps": {},
                }

        elif line_type == "assistant":
            if current_turn is None:
                current_turn = {
                    "user": None,
                    "assistants": [],
                    "tool_results": {},
                    "tool_result_timestamps": {},
                }
            current_turn["assistants"].append(line)

    if current_turn:
        turns.append(current_turn)

    return turns


__all__ = [
    "SOURCE_OFFSET_KEY",
    "extract_session_signals",
    "extract_tool_results",
    "extract_user_content",
    "fold_subagent_skills",
    "group_into_turns",
    "parse_dt",
    "parse_lines",
]
