"""Retained Cursor bubbles can reuse a native tool ID for distinct attempts."""

import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
    parse_cursor_store,
)


def _spans(records):
    data = ("\n".join(json.dumps(row) for row in records) + "\n").encode()
    _, spans = parse_cursor_store(data, trace_id="cursor-reused", user_id="offline")
    return spans


def _records(first_result=False):
    records = [
        {"type": "cursor_store_session", "agentId": "cursor-reused", "createdAt": 1000},
        {"role": "user", "id": "user-one", "content": "First", "createdAt": "2026-09-01T00:00:00Z"},
        {
            "role": "assistant",
            "id": "bubble-one",
            "createdAt": "2026-09-01T00:00:01Z",
            "content": [
                {
                    "type": "tool-call",
                    "toolCallId": "reused",
                    "toolName": "ReadFile",
                    "args": {"path": "one.py"},
                },
            ],
        },
    ]
    if first_result:
        records.append(
            {
                "role": "tool",
                "content": [
                    {"type": "tool-result", "toolCallId": "reused", "result": "first result"}
                ],
            }
        )
    records.extend(
        [
            {
                "role": "user",
                "id": "user-two",
                "content": "Second",
                "createdAt": "2026-09-01T00:00:02Z",
            },
            {
                "role": "assistant",
                "id": "bubble-two",
                "createdAt": "2026-09-01T00:00:03Z",
                "content": [
                    {
                        "type": "tool-call",
                        "toolCallId": "reused",
                        "toolName": "ReadFile",
                        "args": {"path": "two.py"},
                    },
                ],
            },
            {
                "role": "tool",
                "createdAt": "2026-09-01T00:00:04Z",
                "content": [
                    {"type": "tool-result", "toolCallId": "reused", "result": "second result"}
                ],
            },
        ]
    )
    return records


def test_reused_id_preserves_unfinished_attempt_and_pairs_latest_result():
    spans = _spans(_records())
    tools = [s for s in spans if s.attributes.get("tool.name")]
    assert len(tools) == 2
    first, second = tools
    assert first.span_id != second.span_id
    assert first.parent_id != second.parent_id
    assert first.attributes["source.row_cursor"] == "bubble-one"
    assert second.attributes["source.row_cursor"] == "bubble-two"
    assert first.attributes["input.value"] != second.attributes["input.value"]
    assert first.attributes["output.value"] == ""
    assert first.status_code == "UNSET"
    assert second.attributes["output.value"] == "second result"
    assert second.status_code == "OK"
    assert [s.span_id for s in spans] == [s.span_id for s in _spans(_records())]


def test_sequential_reuse_retains_each_completed_occurrence():
    tools = [s for s in _spans(_records(first_result=True)) if s.attributes.get("tool.name")]
    assert len(tools) == len({s.span_id for s in tools}) == 2
    assert [s.attributes["output.value"] for s in tools] == ["first result", "second result"]
    assert all(s.status_code == "OK" for s in tools)
