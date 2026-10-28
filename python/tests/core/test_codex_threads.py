"""Codex Desktop subagents, compactions and interrupts, read from synthetic rollouts.

Codex Desktop writes each subagent as its own rollout. `session_meta` names
who started it (`thread_source`), its parent (`parent_thread_id`), and, for a
forked subagent, where its own lines start after the copied parent history
(`subagent_history_start_ordinal`). A subagent's input is the parent's
`agent_message`, never a user message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agentplay import builder, discover
from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex

PARENT = "01a0a028-0000-7000-8000-0000000000b1"
CHILD = "01a0a028-0000-7000-8000-0000000000b2"
GUARDIAN = "01a0a028-0000-7000-8000-0000000000b3"
WINDOW = "01a0a028-0000-7000-8000-0000000000b4"


def _line(kind, payload, second=0, minute=0):
    return {"type": kind, "timestamp": f"2026-09-08T10:{minute:02}:{second:02}Z", "payload": payload}


def _meta(thread_id, source="user", parent=None, start=None, path=None):
    payload = {
        "id": thread_id,
        "originator": "codex_cli_rs",
        "cwd": "/repo",
        "thread_source": source,
        "parent_thread_id": parent,
    }
    if start is not None:
        payload["subagent_history_start_ordinal"] = start
    if path:
        payload["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "agent_path": path}}}
    return _line("session_meta", payload)


def _user(text, second=1):
    return _line("response_item", {"role": "user", "content": [{"type": "input_text", "text": text}]}, second)


def _agent_message(second=1):
    return _line(
        "response_item", {"type": "agent_message", "content": [{"type": "input_text", "text": "TASK-SECRET"}]}, second
    )


def _call(call_id, name="exec_command", second=2, arguments=None, output="Process exited with code 0"):
    args = json.dumps(arguments or {"cmd": "ls"})
    return [
        _line("response_item", {"type": "function_call", "call_id": call_id, "name": name, "arguments": args}, second),
        _line("response_item", {"type": "function_call_output", "call_id": call_id, "output": output}, second),
    ]


def _usage(inp, cached, out, total_before, second):
    last = dict(input_tokens=inp, cached_input_tokens=cached, output_tokens=out, total_tokens=inp + out)
    total = total_before + inp + out
    cum = dict(input_tokens=total - out, cached_input_tokens=0, output_tokens=out, total_tokens=total)
    return _line(
        "event_msg", {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": cum}}, second
    )


def _bytes(records):
    return b"\n".join(json.dumps(r).encode() for r in records) + b"\n"


def _parse(records):
    return parse_codex(_bytes(records), "local:codex:t")[1]


def _turns(spans):
    return [s for s in spans if s.name.startswith("Turn ")]


def _tools(spans):
    turns = {s.span_id for s in _turns(spans)}
    return [s for s in spans if s.parent_id in turns]


# ------------------------------------------------------------------ bug A
def test_a_subagent_driven_by_agent_message_keeps_its_tool_calls():
    spans = _parse(
        [
            _meta(CHILD, "subagent", PARENT),
            _user("<environment_context>cwd</environment_context>"),
            _agent_message(),
            *_call("c1"),
            *_call("c2", second=3),
            _agent_message(second=4),
            *_call("c3", second=5),
        ]
    )
    assert len(_turns(spans)) == 1
    assert len(_tools(spans)) == 3
    # The parent's instructions are not a user prompt and never reach a span.
    assert _turns(spans)[0].attributes["input.value"] == ""
    assert "TASK-SECRET" not in json.dumps([s.attributes for s in spans], default=str)


def test_agent_message_inside_an_open_turn_does_not_split_it():
    spans = _parse([_meta(PARENT), _user("Fix it"), *_call("c1"), _agent_message(3), *_call("c2", second=4)])
    assert len(_turns(spans)) == 1
    assert len(_tools(spans)) == 2


# ------------------------------------------------------------------ bug B
def test_a_prompt_written_as_a_markdown_heading_opens_a_turn():
    spans = _parse(
        [
            _meta(PARENT),
            _user("<environment_context>cwd</environment_context>"),
            _user("# Plan\nrefactor the parser", second=1),
            *_call("c1"),
        ]
    )
    assert len(_turns(spans)) == 1
    assert len(_tools(spans)) == 1


def test_the_injected_agents_md_block_is_still_not_a_prompt():
    spans = _parse(
        [
            _meta(PARENT),
            _user("# AGENTS.md instructions for /repo\n\n<INSTRUCTIONS>x</INSTRUCTIONS>"),
            _user("Fix it", second=2),
            *_call("c1", second=3),
        ]
    )
    assert [t.attributes["input.value"] for t in _turns(spans)] == ["Fix it"]


def test_the_ide_extension_context_block_is_not_a_prompt():
    spans = _parse(
        [
            _meta(PARENT),
            _user("# Context from my IDE setup:\n\n## Active file: src/app.py\n\n## My request for Codex:\nFix it"),
            _line("event_msg", {"type": "user_message", "message": "Fix it"}, 1),
            *_call("c1"),
        ]
    )
    assert [t.attributes["input.value"] for t in _turns(spans)] == ["Fix it"]
