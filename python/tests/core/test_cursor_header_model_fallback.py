"""Cursor sessions whose messages carry no model still get the header's.

Cursor writes the model
twice — per-message at ``providerOptions.cursor.modelName`` and once in the
file header's ``model`` field — but some sessions carry NO message-level
providerOptions at all. The model must come from the header in that case, or the session
would be model-less and priced $0.
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
    parse_cursor_store,
)


def _transcript(header_model: str, message_model: str | None) -> bytes:
    header = {
        "type": "cursor_store_session",
        "agentId": "abc-123",
        "model": header_model,
        "cwd": "/w/repo",
        "createdAt": "2026-08-19T10:00:00.000Z",
    }
    msg_user: dict = {
        "role": "user",
        "content": "do the thing",
        "createdAt": "2026-08-19T10:00:01.000Z",
    }
    if message_model:
        msg_user["providerOptions"] = {"cursor": {"modelName": message_model}}
    msg_asst = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "createdAt": "2026-08-19T10:00:05.000Z",
    }
    return "\n".join(json.dumps(o) for o in (header, msg_user, msg_asst)).encode()


def _root_model(data: bytes) -> str:
    _trace, spans = parse_cursor_store(data, "local:cursor:abc-123")
    root = next(s for s in spans if s.parent_id is None)
    return root.attributes.get("llm.model_name")


def test_message_model_still_wins_when_present():
    assert _root_model(_transcript("cursor-grok-4.6-high-fast", "grok-4.6")) == "grok-4.6"


def test_header_model_fills_in_when_messages_carry_none():
    assert (
        _root_model(_transcript("cursor-grok-4.6-high-fast", None))
        == "cursor-grok-4.6-high-fast"
    )


def test_auto_default_passes_through_honestly():
    # "default" (Cursor Auto) is not a priceable model, but it is the truth —
    # pricing treats it as unpriceable either way, and the billed-cost stamp
    # is what prices those sessions.
    assert _root_model(_transcript("default", None)) == "default"
