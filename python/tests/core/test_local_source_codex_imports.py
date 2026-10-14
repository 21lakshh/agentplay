"""A transcript Codex imported from another agent is not a Codex session.

Codex can import another agent's sessions. It writes each one as an ordinary `sessions/**/rollout-*.jsonl` and
records the import in `<codex home>/external_agent_session_imports.json`. The
original session is already captured under its own agent, so indexing the copy
counted it a second time -- as a Codex session with no model, $0 and a
total-only token count the parser rightly refuses to price.

The skip needs BOTH halves, and these tests pin each one:

* the thread is in Codex's own import ledger -- never a guess from the file's
  shape, so a real session that merely looks empty is still captured; and
* Codex never ran a turn of its own in it (no `turn_context` record) -- an
  import the developer then CONTINUED in Codex carries real Codex spend and
  must still be captured.

Anything wrong with the ledger fails open: every rollout is indexed, exactly as
before the ledger was consulted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.source import (
    LocalSource,
    _codex_ran_a_turn,
)

IMPORTED = "01a0a028-0000-7000-8000-00000000000a"
CONTINUED = "01a0a028-0000-7000-8000-00000000000b"
REAL = "01a0a028-0000-7000-8000-00000000000c"
REAL_BUT_EMPTY = "01a0a028-0000-7000-8000-00000000000d"

_META = {"type": "session_meta", "payload": {"originator": "codex_cli_rs", "cwd": "/repo"}}
_TURN = {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}}
# What an import carries: the ORIGINAL session's size as a total-only count.
_TOTAL_ONLY_USAGE = {
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {
            "total_token_usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 493481}
        },
    },
}
_REAL_USAGE = {
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {
            "total_token_usage": {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}
        },
    },
}


def _session_id(thread_id: str) -> str:
    return f"local:codex:rollout-2026-09-08T07-23-37-{thread_id}"


def _write_rollout(home: Path, thread_id: str, records: list[dict]) -> None:
    day = home / "sessions" / "2026" / "09" / "08"
    day.mkdir(parents=True, exist_ok=True)
    body = b"\n".join(json.dumps(r).encode() for r in records) + b"\n"
    (day / f"rollout-2026-09-08T07-23-37-{thread_id}.jsonl").write_bytes(body)


def _codex_home(tmp_path: Path) -> Path:
    home = tmp_path / ".codex"
    _write_rollout(home, IMPORTED, [_META, _TOTAL_ONLY_USAGE])
    _write_rollout(home, CONTINUED, [_META, _TOTAL_ONLY_USAGE, _TURN, _REAL_USAGE])
    _write_rollout(home, REAL, [_META, _TURN, _REAL_USAGE])
    _write_rollout(home, REAL_BUT_EMPTY, [_META])
    return home


def _ledger(*thread_ids: str) -> str:
    return json.dumps(
        {
            "records": [
                {"imported_thread_id": t, "source_path": "/home/dev/.claude/projects/-repo/x.jsonl"}
                for t in thread_ids
            ],
            "detected_connector_records": [],
        }
    )


def _discovered(home: Path) -> set[str]:
    source = LocalSource(claude_code_homes=[], codex_homes=[home])
    return {d.session_id for d in source.discover_sessions()}


def test_only_the_imported_and_never_continued_rollout_is_skipped(tmp_path: Path) -> None:
    home = _codex_home(tmp_path)
    (home / "external_agent_session_imports.json").write_text(_ledger(IMPORTED, CONTINUED))

    source = LocalSource(claude_code_homes=[], codex_homes=[home])
    found = {d.session_id for d in source.discover_sessions()}

    assert found == {_session_id(CONTINUED), _session_id(REAL), _session_id(REAL_BUT_EMPTY)}
    assert source.discovery_stats()["codex"] == {
        "seen": 4,
        "indexed": 3,
        "skipped": {"imported_not_continued": 1},
    }


@pytest.mark.parametrize(
    "ledger",
    [
        None,  # no ledger at all: Codex never imported anything
        "{not json",
        "[]",
        '{"records": "nope"}',
        '{"records": [null, 7, {"imported_thread_id": null}, {"title": "no id"}]}',
    ],
)
def test_a_missing_or_malformed_ledger_skips_nothing(tmp_path: Path, ledger: str | None) -> None:
    home = _codex_home(tmp_path)
    if ledger is not None:
        (home / "external_agent_session_imports.json").write_text(ledger)

    assert _discovered(home) == {
        _session_id(IMPORTED),
        _session_id(CONTINUED),
        _session_id(REAL),
        _session_id(REAL_BUT_EMPTY),
    }


def test_valid_records_still_count_beside_malformed_ones(tmp_path: Path) -> None:
    home = _codex_home(tmp_path)
    (home / "external_agent_session_imports.json").write_text(
        json.dumps({"records": [None, {"imported_thread_id": 7}, {"imported_thread_id": IMPORTED}]})
    )

    assert _session_id(IMPORTED) not in _discovered(home)


def test_a_rollout_that_cannot_be_read_is_never_the_reason_to_skip(tmp_path: Path) -> None:
    # Gone between the head read and the scan: "ran a turn" keeps it captured.
    assert _codex_ran_a_turn(tmp_path / "vanished.jsonl") is True


def test_a_transcript_that_only_mentions_turn_context_is_still_an_import(tmp_path: Path) -> None:
    # Only a record whose OWN type is `turn_context` is a Codex turn. An imported
    # conversation about Codex rollouts quotes the name in its text, and another
    # record can carry it as a value.
    home = tmp_path / ".codex"
    quoted_in_text = {
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": 'grep "turn_context" x.jsonl'},
    }
    carried_as_a_value = {"type": "event_msg", "payload": {"type": "turn_context"}}
    _write_rollout(home, IMPORTED, [_META, quoted_in_text, carried_as_a_value, _TOTAL_ONLY_USAGE])
    (home / "external_agent_session_imports.json").write_text(_ledger(IMPORTED))

    assert _discovered(home) == set()
