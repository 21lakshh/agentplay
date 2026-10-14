"""Cursor skill invocation detection.

Cursor has no dedicated ``Skill``/``activate_skill`` tool — that vocabulary is
Claude Code's own, and checking for it (as ``cursor_agent_transcript.py``
already did) essentially never fires for genuine Cursor usage. Cursor's own
skills system (``~/.cursor/skills-cursor/<name>/SKILL.md``,
``~/.codex/skills/**/SKILL.md``, or a marketplace-plugin cache path) is
invoked by the model reading the skill's SKILL.md file with a plain Read call,
per Cursor's own injected instructions. Confirmed against a real local Cursor
IDE history capture — a ``read_file_v2`` Read call whose result carried the
skill's frontmatter'd body. ``cursor_store.py`` (the dominant format — CLI
``store.db`` + IDE composer ``vscdb``) detects skills from file reads;
``cursor_agent_transcript.py`` does the same instead of relying on the tool name.

These tests lock in: name detection is basename-based (not tied to one root
directory), counts aggregate per canonical skill, a Read of an unrelated file
(including one merely containing "skills/" in its path) is never a false
positive, and the captured body reaches ``skill_definitions``.

IDE composer ``read_file_v2`` calls carry the touched path under
``"targetFile"``, not ``"path"``. `_skill_read_messages` below uses
`targetFile` to match; `test_cursor_store_detects_a_real_skill_read_via_target_file_key`
pins it.
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources._base import build_sidecar_signals
from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_agent_transcript import (
    parse_cursor_agent_transcript,
)
from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
    parse_cursor_store,
)

# --- cursor_store.py (dominant format) --------------------------------------


def _cursor_transcript(*lines: dict) -> bytes:
    header = {
        "type": "cursor_store_session",
        "agentId": "agent-1",
        "createdAt": 1_000_000,
        "updatedAt": 1_010_000,
        "cwd": "/w/repo",
    }
    return ("\n".join(json.dumps(x) for x in (header, *lines))).encode()


def _signals(data: bytes) -> dict:
    _trace, spans = parse_cursor_store(data, "local:cursor:agent-1")
    root = next(s for s in spans if s.parent_id is None)
    raw = root.attributes.get("harness.session_signals")
    return json.loads(raw) if raw else {}


# Real capture: a marketplace-plugin skill cached under a
# hash-named directory, `read_file_v2` (already aliased to "Read"), result a
# JSON-encoded string wrapping {"contents": "..."}.
_SKILL_PATH = (
    "/home/user/.cursor/plugins/cache/cursor-public/deploy-on-aws/"
    "437f4bed0bfd7367df7613ca932fe900f7e08d89/skills/deploy/SKILL.md"
)
_SKILL_BODY = '---\nname: deploy\ndescription: "Deploy applications to AWS."\n---\n\nBody text.'


def _skill_read_messages(
    path: str = _SKILL_PATH, body: str = _SKILL_BODY, arg_key: str = "path"
) -> tuple[dict, dict]:
    call = {
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "tc1",
                "toolName": "read_file_v2",
                "args": {arg_key: path},
            }
        ],
    }
    result = {
        "role": "tool",
        "content": [
            {
                "type": "tool-result",
                "toolCallId": "tc1",
                "result": json.dumps({"contents": body}),
            }
        ],
    }
    return call, result


def test_cursor_store_detects_a_real_skill_read():
    call, result = _skill_read_messages()
    data = _cursor_transcript({"role": "user", "content": "deploy this"}, call, result)
    signals = _signals(data)
    assert signals["skills_used"] == [{"path": "deploy", "count": 1}]
    assert signals["skill_definitions"] == [{"name": "deploy", "body": _SKILL_BODY}]


def test_cursor_store_matches_any_root_by_basename():
    """Not tied to skills-cursor/ — a .codex/skills/ path counts the same."""
    call, result = _skill_read_messages(path="/home/user/.codex/skills/.system/skill-creator/SKILL.md")
    data = _cursor_transcript({"role": "user", "content": "hi"}, call, result)
    assert _signals(data)["skills_used"] == [{"path": "skill-creator", "count": 1}]


def test_cursor_store_counts_repeat_reads_of_the_same_skill():
    call1, result1 = _skill_read_messages()
    call2 = json.loads(json.dumps(call1))
    call2["content"][0]["toolCallId"] = "tc2"
    result2 = json.loads(json.dumps(result1))
    result2["content"][0]["toolCallId"] = "tc2"
    data = _cursor_transcript(
        {"role": "user", "content": "go"}, call1, result1, call2, result2
    )
    assert _signals(data)["skills_used"] == [{"path": "deploy", "count": 2}]


def test_cursor_store_ignores_a_read_of_an_unrelated_file():
    call, result = _skill_read_messages(path="/w/repo/main.py", body="print('hi')")
    data = _cursor_transcript({"role": "user", "content": "read main"}, call, result)
    assert _signals(data)["skills_used"] == []


def test_cursor_store_requires_the_basename_not_just_a_skills_substring():
    """A path containing 'skills/' that isn't actually a SKILL.md read must
    not false-positive (e.g. reading a project's own src/skills/utils.py)."""
    call, result = _skill_read_messages(path="/w/repo/src/skills/utils.py", body="def f(): ...")
    data = _cursor_transcript({"role": "user", "content": "read utils"}, call, result)
    assert _signals(data)["skills_used"] == []


def test_cursor_store_skill_detection_is_case_insensitive_on_basename():
    call, result = _skill_read_messages(path="/w/repo/skills/loop/Skill.MD")
    data = _cursor_transcript({"role": "user", "content": "go"}, call, result)
    assert _signals(data)["skills_used"] == [{"path": "loop", "count": 1}]


def test_cursor_store_a_non_read_tool_never_counts_as_a_skill():
    call = {
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "tc1",
                "toolName": "run_terminal_cmd",
                "args": {"command": "cat /w/repo/skills/deploy/SKILL.md"},
            }
        ],
    }
    result = {
        "role": "tool",
        "content": [{"type": "tool-result", "toolCallId": "tc1", "result": "ok"}],
    }
    data = _cursor_transcript({"role": "user", "content": "cat it"}, call, result)
    assert _signals(data)["skills_used"] == []


def test_cursor_store_dangling_skill_call_with_no_result_still_counts_the_name():
    """A skill Read with no matching result (session ended mid-call) still
    gets a best-effort name capture, just with an empty body."""
    call, _result = _skill_read_messages()
    data = _cursor_transcript({"role": "user", "content": "go"}, call)
    signals = _signals(data)
    assert signals["skills_used"] == [{"path": "deploy", "count": 1}]
    assert signals["skill_definitions"] == []


# --- IDE composer's real `*_v2` arg keys, not `"path"`/`"paths"` --


def test_cursor_store_detects_a_real_skill_read_via_target_file_key():
    """IDE composer `read_file_v2` calls carry the path under `"targetFile"`,
    never under `"path"` (the key the fixture above assumes). This is the
    shape that must be detected."""
    call, result = _skill_read_messages(arg_key="targetFile")
    data = _cursor_transcript({"role": "user", "content": "deploy this"}, call, result)
    signals = _signals(data)
    assert signals["skills_used"] == [{"path": "deploy", "count": 1}]
    assert signals["skill_definitions"] == [{"name": "deploy", "body": _SKILL_BODY}]


def test_cursor_store_normalizes_edit_file_v2_relative_workspace_path():
    """Same gap on the sibling `edit_file_v2` tool (aliased to
    `"Edit"`): calls carry the path under
    `"relativeWorkspacePath"`, never `"path"`/`"paths"`/`"file_path"`. Not a
    skill read, but exercises the same `_normalize_file_path_arg` path that
    also feeds `file_paths.py`'s read/edit/search counters."""
    from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
        _normalize_file_path_arg,
    )

    args = _normalize_file_path_arg({"relativeWorkspacePath": "/w/repo/main.py"})
    assert args["file_path"] == "/w/repo/main.py"


def test_cursor_store_file_path_survives_truncation_of_a_large_edit():
    """`_build_tool_span` truncates the JSON-serialized args to `_TRUNCATE` (2000) chars for `input.value`, which
    `file_paths.py`'s regex then scans. `edit_file_v2`'s real args carry the
    whole diff under `streamingContent` — easily past that limit on its own
    — so a `file_path` appended AFTER it would be sheared off by truncation
    before anything ever reads it. `_normalize_file_path_arg` must PREPEND
    the key so it survives regardless of how large the rest of args is."""
    from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
        _normalize_file_path_arg,
    )

    args = _normalize_file_path_arg(
        {
            "relativeWorkspacePath": "/w/repo/main.py",
            "streamingContent": "x" * 5000,
        }
    )
    serialized = json.dumps(args)
    assert '"file_path": "/w/repo/main.py"' in serialized[:2000]


# --- a real Read result can be a plain (non-JSON) string body ----


def test_cursor_store_captures_a_plain_string_result_as_the_literal_body():
    """A result may carry `"result"` as the raw file content directly (not JSON,
    not wrapped in `{"contents": ...}`) - e.g. frontmatter starting with `---`,
    which fails `json.loads`. The raw string is kept as the literal body."""
    plain_body = "---\nname: deploy\ndescription: Deploy to AWS.\n---\n\nBody text."
    call = {
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "tc1",
                "toolName": "read_file_v2",
                "args": {"targetFile": _SKILL_PATH},
            }
        ],
    }
    result = {
        "role": "tool",
        "content": [{"type": "tool-result", "toolCallId": "tc1", "result": plain_body}],
    }
    data = _cursor_transcript({"role": "user", "content": "deploy this"}, call, result)
    assert _signals(data)["skill_definitions"] == [{"name": "deploy", "body": plain_body}]


def test_cursor_store_a_failed_skill_read_never_stores_the_error_as_a_body():
    """A failed read (file missing, permission
    denied, ...) still names the attempted skill, but its `"result"` is an
    error string, not file content. ANY non-JSON string is treated
    as a literal body — without an error check, this would store the error
    message itself as the skill's body. Must fall back to the same empty-
    body contract as a dangling call with no result at all."""
    call = {
        "role": "assistant",
        "content": [
            {
                "type": "tool-call",
                "toolCallId": "tc1",
                "toolName": "read_file_v2",
                "args": {"targetFile": _SKILL_PATH},
            }
        ],
    }
    result = {
        "role": "tool",
        "content": [
            {
                "type": "tool-result",
                "toolCallId": "tc1",
                "result": "Error: ENOENT: no such file or directory",
                "isError": True,
            }
        ],
    }
    data = _cursor_transcript({"role": "user", "content": "deploy this"}, call, result)
    signals = _signals(data)
    assert signals["skills_used"] == [{"path": "deploy", "count": 1}]
    assert signals["skill_definitions"] == []


# --- build_sidecar_signals forwarding (pre-existing gap, fixed alongside) --


def test_sidecar_forwards_captured_skill_definitions():
    """skill_definitions was missing from build_sidecar_signals'
    key allowlist from the day the field was added -- this affects EVERY
    source using the shared helper, not just Cursor. A captured body must
    actually reach the sidecar dict, not just harness.session_signals."""
    call, result = _skill_read_messages()
    data = _cursor_transcript({"role": "user", "content": "deploy this"}, call, result)
    _trace, spans = parse_cursor_store(data, "local:cursor:agent-1")
    sidecar = build_sidecar_signals(spans)
    assert sidecar["skill_definitions"] == [{"name": "deploy", "body": _SKILL_BODY}]
    assert sidecar["skills_used"] == [{"path": "deploy", "count": 1}]


def test_sidecar_omits_skill_definitions_key_when_none_were_captured():
    """Absent, not an empty list -- matches the other signal keys' contract
    (build_sidecar_signals only ever sets a key when `sig.get(key)` is truthy)."""
    data = _cursor_transcript({"role": "user", "content": "hi"})
    _trace, spans = parse_cursor_store(data, "local:cursor:agent-1")
    assert "skill_definitions" not in build_sidecar_signals(spans)


# --- cursor_agent_transcript.py (secondary fix) -----------------------------


def _line(role: str, blocks: list[dict]) -> dict:
    return {"role": role, "message": {"content": blocks}}


def _agent_transcript(*lines: dict) -> bytes:
    return "\n".join(json.dumps(line) for line in lines).encode()


def _agent_transcript_signals(data: bytes) -> dict:
    _trace, spans = parse_cursor_agent_transcript(data, "local:cursor:agent-2")
    root = next(s for s in spans if s.parent_id is None)
    raw = root.attributes.get("harness.session_signals")
    return json.loads(raw) if raw else {}


def test_agent_transcript_detects_a_skill_read_via_path_not_tool_name():
    """The pre-existing _SKILL_TOOL_NAMES check only matches Claude Code's own
    tool name ('skill'/'activate_skill') and never fires for Cursor's real
    'Read' call — this is the actual signal that needs to work."""
    data = _agent_transcript(
        _line("user", [{"type": "text", "text": "<user_query>deploy</user_query>"}]),
        _line(
            "assistant",
            [
                {
                    "type": "tool_use",
                    "name": "ReadFile",
                    "input": {"path": "/home/user/.cursor/skills-cursor/loop/SKILL.md"},
                    "id": "t1",
                }
            ],
        ),
    )
    assert _agent_transcript_signals(data)["skills_used"] == [{"path": "loop", "count": 1}]


def test_agent_transcript_read_of_unrelated_file_is_not_a_skill():
    data = _agent_transcript(
        _line("user", [{"type": "text", "text": "<user_query>read it</user_query>"}]),
        _line(
            "assistant",
            [{"type": "tool_use", "name": "Read", "input": {"path": "/a.py"}, "id": "t1"}],
        ),
    )
    assert _agent_transcript_signals(data)["skills_used"] == []


def test_agent_transcript_legacy_tool_name_check_still_works():
    """Regression guard: the original skill/activate_skill tool-name path
    (kept as a defensive fallback) must keep working unchanged."""
    data = _agent_transcript(
        _line("user", [{"type": "text", "text": "<user_query>go</user_query>"}]),
        _line(
            "assistant",
            [{"type": "tool_use", "name": "skill", "input": {"skill": "entloop"}, "id": "t1"}],
        ),
    )
    assert _agent_transcript_signals(data)["skills_used"] == [{"path": "entloop", "count": 1}]
