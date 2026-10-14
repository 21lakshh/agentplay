"""An archived Codex thread is still a Codex session.

Codex MOVES a rollout, unchanged, from `sessions/YYYY/MM/DD/` to a flat
`archived_sessions/` when the developer archives the thread .
`LocalSource` walked `sessions/` only, so an archived session could never be
found again on a later scan, and one archived before its first scan was
never captured at all.

The session id is built from the file stem, which the move does not change, so
reading the second folder cannot mint a second session for the same thread.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentplay._core.ingestion.coding_agent._sources.local.source import (
    LocalSource,
    codex_rollout_dirs,
)

LIVE = "01a0a028-0000-7000-8000-0000000000a1"
ARCHIVED = "01a0a028-0000-7000-8000-0000000000a2"
ARCHIVED_IMPORT = "01a0a028-0000-7000-8000-0000000000a3"

_META = {"type": "session_meta", "payload": {"originator": "codex_cli_rs", "cwd": "/repo"}}
_TURN = {"type": "turn_context", "payload": {"model": "gpt-6-astra"}}


def _name(thread_id: str) -> str:
    return f"rollout-2026-09-08T22-45-23-{thread_id}.jsonl"


def _write(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join(json.dumps(r).encode() for r in records) + b"\n")


def _session_id(thread_id: str) -> str:
    return f"local:codex:rollout-2026-09-08T22-45-23-{thread_id}"


def _discovered(home: Path) -> set[str]:
    source = LocalSource(claude_code_homes=[], codex_homes=[home])
    return {d.session_id for d in source.discover_sessions()}


def test_an_archived_rollout_is_discovered_under_the_same_session_id(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write(home / "sessions" / "2026" / "09" / "08" / _name(LIVE), [_META, _TURN])
    _write(home / "archived_sessions" / _name(ARCHIVED), [_META, _TURN])

    source = LocalSource(claude_code_homes=[], codex_homes=[home])
    found = {d.session_id for d in source.discover_sessions()}

    assert found == {_session_id(LIVE), _session_id(ARCHIVED)}
    assert source.discovery_stats()["codex"] == {"seen": 2, "indexed": 2, "skipped": {}}


def test_a_home_holding_only_archived_rollouts_is_still_walked(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write(home / "archived_sessions" / _name(ARCHIVED), [_META, _TURN])

    assert _discovered(home) == {_session_id(ARCHIVED)}
    assert codex_rollout_dirs(home) == [home / "archived_sessions"]


def test_the_import_skip_applies_to_an_archived_rollout_too(tmp_path: Path) -> None:
    home = tmp_path / ".codex"
    _write(home / "archived_sessions" / _name(ARCHIVED), [_META, _TURN])
    _write(home / "archived_sessions" / _name(ARCHIVED_IMPORT), [_META])
    (home / "external_agent_session_imports.json").write_text(
        json.dumps({"records": [{"imported_thread_id": ARCHIVED_IMPORT}]})
    )

    assert _discovered(home) == {_session_id(ARCHIVED)}


def test_one_thread_in_both_folders_is_one_session_and_the_fuller_copy_wins(tmp_path: Path) -> None:
    # Codex moves rather than copies, so this should not happen; if a tool ever
    # leaves both behind, the index must not pick the truncated one.
    home = tmp_path / ".codex"
    stale = home / "sessions" / "2026" / "09" / "08" / _name(LIVE)
    full = home / "archived_sessions" / _name(LIVE)
    _write(stale, [_META])
    _write(full, [_META, _TURN, _TURN])

    source = LocalSource(claude_code_homes=[], codex_homes=[home])
    found = [d.session_id for d in source.discover_sessions()]

    assert found == [_session_id(LIVE)]
    assert source._build_index()[_session_id(LIVE)].path == full


def test_a_directory_with_neither_folder_is_not_a_codex_home(tmp_path: Path) -> None:
    (tmp_path / ".codex" / "log").mkdir(parents=True)

    assert codex_rollout_dirs(tmp_path / ".codex") == []
    assert _discovered(tmp_path / ".codex") == set()


_SPAN_RECORDS = [
    {
        "timestamp": "2026-09-08T22:45:23.000Z",
        "type": "session_meta",
        "payload": {"originator": "codex_cli_rs", "cwd": "/repo"},
    },
    {
        "timestamp": "2026-09-08T22:45:24.000Z",
        "type": "turn_context",
        "payload": {"model": "gpt-6-astra", "cwd": "/repo"},
    },
    {
        "timestamp": "2026-09-08T22:45:25.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    },
    {
        "timestamp": "2026-09-08T22:45:26.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi"}],
        },
    },
]


def test_a_rollout_archived_after_indexing_is_re_resolved_and_read(tmp_path: Path, caplog) -> None:
    """A rollout that moved (was archived) between discovery and read is found again."""
    home = tmp_path / ".codex"
    live = home / "sessions" / "2026" / "09" / "08" / _name(LIVE)
    _write(live, _SPAN_RECORDS)
    source = LocalSource(claude_code_homes=[], codex_homes=[home])
    assert {d.session_id for d in source.discover_sessions()} == {_session_id(LIVE)}

    (home / "archived_sessions").mkdir()
    live.rename(home / "archived_sessions" / _name(LIVE))

    with caplog.at_level("INFO"):
        spans = source.get_spans_by_session(_session_id(LIVE))

    assert len(spans) == 2
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    # Counts describe the rebuilt walk, not both walks added together.
    assert source.discovery_stats()["codex"] == {"seen": 1, "indexed": 1, "skipped": {}}


def test_a_rollout_deleted_after_indexing_returns_nothing_without_an_error(
    tmp_path: Path, caplog
) -> None:
    home = tmp_path / ".codex"
    live = home / "sessions" / "2026" / "09" / "08" / _name(LIVE)
    _write(live, _SPAN_RECORDS)
    source = LocalSource(claude_code_homes=[], codex_homes=[home])
    list(source.discover_sessions())
    live.unlink()

    with caplog.at_level("INFO"):
        spans = source.get_spans_by_session(_session_id(LIVE))

    assert spans == []
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("moved or deleted" in r.getMessage() for r in caplog.records)
