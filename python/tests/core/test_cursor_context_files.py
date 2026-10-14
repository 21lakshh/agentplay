"""Cursor composer sessions forward their referenced file paths.

Composer chats rarely run ``git remote``, rarely open a PR, and carry few
tool-call file paths, so every git-free repo-attribution tier missed them and
they ended up with no repo attribution. Cursor does record the
files, in ``newlyCreatedFiles``, ``context.selections``, the
``originalFileStates`` keys, and the git worktree / tracked-repo roots, but the
materializer dropped all of it, so nothing reached the parser.

Measured on a real ``state.vscdb``: 6 of 189 composer records carried a
``workspaceIdentifier`` cwd; 66 carried one of the above. The materializer now collects
them into the header's ``contextFiles`` and this parser forwards them as
``harness.context_files`` for repo attribution.
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
    parse_cursor_store,
)


def _transcript(**header_extra) -> bytes:
    header = {
        "type": "cursor_store_session",
        "agentId": "abc-123",
        "cwd": "/w/repo",
        "createdAt": "2026-08-19T10:00:00.000Z",
        **header_extra,
    }
    msg_user = {
        "role": "user",
        "content": "do the thing",
        "createdAt": "2026-08-19T10:00:01.000Z",
    }
    msg_asst = {
        "role": "assistant",
        "content": [{"type": "text", "text": "done"}],
        "createdAt": "2026-08-19T10:00:05.000Z",
    }
    return "\n".join(json.dumps(o) for o in (header, msg_user, msg_asst)).encode()


def _context_files(data: bytes):
    _trace, spans = parse_cursor_store(data, "local:cursor:abc-123")
    root = next(s for s in spans if s.parent_id is None)
    return root.attributes.get("harness.context_files")


def test_context_files_reach_the_root_span():
    paths = ["/w/repo/a.py", "/w/repo/b.py"]
    assert json.loads(_context_files(_transcript(contextFiles=paths))) == paths


def test_attribute_is_absent_when_the_cli_sent_none():
    """Absent, not empty: a reader must tell "no paths" from "old CLI build"."""
    assert _context_files(_transcript()) is None
    assert _context_files(_transcript(contextFiles=[])) is None


def test_non_string_entries_are_dropped():
    data = _transcript(contextFiles=["/w/repo/a.py", 42, None, ""])
    assert json.loads(_context_files(data)) == ["/w/repo/a.py"]


def test_a_malformed_field_does_not_break_the_parse():
    """A bad header field must never cost us the whole session."""
    data = _transcript(contextFiles="not-a-list")
    _trace, spans = parse_cursor_store(data, "local:cursor:abc-123")
    assert spans, "session must still parse"
    assert _context_files(data) is None


def test_windows_paths_survive_verbatim():
    """Normalisation belongs to the consumer (`_paths.match_key`), not here."""
    paths = [r"C:\Users\Ada\repo\main.py"]
    assert json.loads(_context_files(_transcript(contextFiles=paths))) == paths
