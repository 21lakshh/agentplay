"""Claude Code subagent edits reach code-authorship replay without becoming tool calls.

Subagent (Agent/Workflow tool) traffic is folded into the parent transcript as
``isSidechain: true`` lines. Its edits ship on the root span's
``code.subagent_edit_calls`` (the envelope's separate ``subagent_edit_calls``
array), never as tool spans, so every span-derived tool metric stays
parent-only. Fixtures mirror real transcript shapes with synthetic content.
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.claude_code import (
    parse_claude_code,
)
from agentplay._core.ingestion.coding_agent.metrics import session_tool_distribution


def _line(kind: str, at: str, content: list | str, **extra) -> dict:
    return {
        "type": kind,
        "timestamp": f"2026-09-20T10:00:{at}Z",
        "sessionId": "sid",
        "cwd": "/repo",
        "message": {"role": kind, "content": content},
        **extra,
    }


def _use(call_id: str, name: str, tool_input: dict) -> list[dict]:
    return [{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}]


def _result(call_id: str, *, error: bool = False) -> list[dict]:
    return [{"type": "tool_result", "tool_use_id": call_id, "content": "ok", "is_error": error}]


def _edit(path: str) -> tuple[dict, dict]:
    args = {"file_path": path, "old_string": "old", "new_string": "new"}
    native = {
        "filePath": path,
        "oldString": "old",
        "newString": "new",
        "userModified": False,
        "replaceAll": False,
        "structuredPatch": [
            {
                "oldStart": 1,
                "oldLines": 2,
                "newStart": 1,
                "newLines": 2,
                "lines": ["-return old", "+return new", " context"],
            }
        ],
    }
    return args, native


def _parent() -> list[dict]:
    edit_args, edit_native = _edit("/repo/app.py")
    return [
        _line("user", "00", "ship it"),
        _line(
            "assistant",
            "01",
            _use("toolu_agent", "Agent", {"prompt": "p", "run_in_background": True}),
        ),
        _line(
            "user",
            "02",
            _result("toolu_agent"),
            toolUseResult={"status": "async_launched", "agentId": "a1", "isAsync": True},
        ),
        _line("assistant", "10", _use("toolu_parent_edit", "Edit", edit_args)),
        _line("user", "11", _result("toolu_parent_edit"), toolUseResult=edit_native),
    ]


def _subagent(agent_id: str = "a1") -> list[dict]:
    edit_args, edit_native = _edit("/repo/lib.py")
    side = {"isSidechain": True, "agentId": agent_id}
    parent_edit_args, _ = _edit("/repo/app.py")
    return [
        _line("user", "02", "subagent prompt", **side),
        _line("assistant", "05", _use("toolu_sub_edit", "Edit", edit_args), **side),
        _line("user", "06", _result("toolu_sub_edit"), toolUseResult=edit_native, **side),
        _line(
            "assistant",
            "20",
            _use("toolu_sub_write", "Write", {"file_path": "/repo/new.py", "content": "x\n"}),
            **side,
        ),
        _line(
            "user",
            "21",
            _result("toolu_sub_write"),
            toolUseResult={"type": "create", "filePath": "/repo/new.py"},
            **side,
        ),
        _line("assistant", "22", _use("toolu_sub_failed", "Edit", edit_args), **side),
        _line("user", "23", _result("toolu_sub_failed", error=True), **side),
        # Claude Code copies a spawning parent message into subagent files: a
        # parent-recorded call id is the parent's, never a subagent edit.
        _line("assistant", "24", _use("toolu_parent_edit", "Edit", parent_edit_args), **side),
    ]


def _parse(lines: list[dict]):
    data = "\n".join(json.dumps(line) for line in lines).encode()
    return parse_claude_code(data, "t")[1]


def _tool_spans(spans) -> list[tuple]:
    return [
        (s.span_id, s.attributes["tool.name"], s.status_code)
        for s in spans
        if s.attributes.get("tool.name")
    ]


def test_subagent_edits_ship_for_replay_without_changing_tool_spans():
    parent_only = _parse(_parent())
    folded = _parse(_parent() + _subagent())

    # Every span-derived tool metric reads `tool.name` spans: unchanged.
    assert _tool_spans(folded) == _tool_spans(parent_only)
    assert session_tool_distribution(folded) == {"Agent": 1, "Edit": 1}
    assert "code.subagent_edit_calls" not in parent_only[0].attributes

    calls = folded[0].attributes["code.subagent_edit_calls"]
    assert [c["native_call_id"] for c in calls] == ["toolu_sub_edit", "toolu_sub_write"]
    assert {c["subagent_id"] for c in calls} == {"a1"}
    assert {c["turn_id"] for c in calls} == {"t:turn:1"}
    assert [[e["operation"] for e in c["edit_evidence"]["edits"]] for c in calls] == [
        ["replace"],
        ["write"],
    ]
    assert all(c["edit_evidence"]["status"] == "captured" for c in calls)
    assert all(json.loads(c["input_value"])["file_path"] for c in calls)


def test_subagent_edits_keep_their_real_completion_order_around_parent_edits():
    folded = _parse(_parent() + _subagent())
    parent_edit = next(s for s in folded if s.attributes.get("tool.name") == "Edit")
    events = sorted(
        [(parent_edit.end_time.isoformat(), "parent")]
        + [
            (c["completed_at"], c["native_call_id"])
            for c in folded[0].attributes["code.subagent_edit_calls"]
        ]
    )
    # The background subagent's first edit finished before the parent's edit
    # and its second after it; its spawning call had returned at :02.
    assert [name for _, name in events] == ["toolu_sub_edit", "parent", "toolu_sub_write"]


def test_echoed_call_whose_result_is_only_in_the_sidechain_still_ships():
    edit_args, edit_native = _edit("/repo/echo.py")
    side = {"isSidechain": True, "agentId": "a1"}
    folded = _parse(
        _parent()
        + _subagent()
        + [
            # The call is echoed into the parent, its result is not.
            _line("assistant", "30", _use("toolu_echoed", "Edit", edit_args)),
            _line("assistant", "30", _use("toolu_echoed", "Edit", edit_args), **side),
            _line("user", "31", _result("toolu_echoed"), toolUseResult=edit_native, **side),
        ]
    )
    calls = folded[0].attributes["code.subagent_edit_calls"]
    (echoed,) = (c for c in calls if c["native_call_id"] == "toolu_echoed")
    assert echoed["edit_evidence"]["status"] == "captured"
