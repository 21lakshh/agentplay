"""LocalSource wiring for Copilot in VS Code (Chat/agent mode) --
agentplay._core half. The parser (`parse_copilot_vscode`) and
its `detect_agent` branch are tested elsewhere (test_copilot_vscode_parser.py,
test_parser_invariants.py); this file pins that a real file on disk actually
gets discovered, classified, deduped and dispatched end to end.

# Why this exists

The .json branch has a real gap other agents don't: `_read_head`'s DEFAULT
behaviour (no `stop_when`) stops as soon as 20 lines accumulate, which is the
right trade for the .jsonl branch (only the first line matters) but wrong for
a pretty-printed whole document -- a real 1.6KB local Copilot .json session
with ZERO requests was already 66 lines. Reading a large one with the default
would truncate it, and `json.loads` on the truncated blob would then always
fail, silently dropping the session from discovery forever (the exact class
of bug `_read_head`'s own docstring already describes fixing twice for other
agents). `test_a_large_json_document_past_the_default_line_cap_is_not_truncated`
pins the fix.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agentplay._core.ingestion.coding_agent._sources.local.source import LocalSource


def _snapshot_line(session_id: str, requests: list[dict]) -> bytes:
    return (
        json.dumps({"kind": 0, "v": {"sessionId": session_id, "requests": requests}}) + "\n"
    ).encode()


def _snapshot_line_padded(session_id: str, requests: list[dict], target_len: int) -> bytes:
    """Like `_snapshot_line`, but padded with an extra `_pad` field so the
    encoded line is exactly `target_len` bytes -- for fixtures that must
    differ in content while being byte-identical in size."""
    unpadded = (
        json.dumps({"kind": 0, "v": {"sessionId": session_id, "requests": requests, "_pad": ""}})
        + "\n"
    ).encode()
    needed = target_len - len(unpadded)
    assert needed >= 0, f"target_len {target_len} smaller than the unpadded fixture ({len(unpadded)}B)"
    padded = (
        json.dumps(
            {
                "kind": 0,
                "v": {"sessionId": session_id, "requests": requests, "_pad": "x" * needed},
            }
        )
        + "\n"
    ).encode()
    assert len(padded) == target_len
    return padded


def _splice_line(request: dict) -> bytes:
    return (json.dumps({"kind": 2, "k": ["requests"], "v": [request]}) + "\n").encode()


def _request(request_id: str, ts_ms: int = 1) -> dict:
    return {"requestId": request_id, "timestamp": ts_ms, "elapsedMs": 1, "result": {}}


class TestDiscoveryAndDispatch:
    def test_a_real_jsonl_snapshot_session_is_discovered_and_parses(self, tmp_path: Path):
        home = tmp_path / "Code" / "User"
        ws = home / "workspaceStorage" / "abc123" / "chatSessions"
        ws.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000001"
        session_id = f"local:copilot-vscode:{sid}"
        (ws / f"{sid}.jsonl").write_bytes(_snapshot_line(sid, [_request("r1")]))

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        found = source.discover_sessions()

        assert len(found) == 1
        assert source.get_agent_type(session_id) == "copilot-vscode"
        assert source.discovery_stats()["copilot-vscode"] == {
            "seen": 1,
            "indexed": 1,
            "skipped": {},
        }
        spans = source.get_spans_by_session(session_id)
        assert any(s.name == "Turn 1" for s in spans), "parser was never dispatched"

    def test_a_real_json_document_session_is_discovered_and_parses(self, tmp_path: Path):
        home = tmp_path / "Code" / "User"
        ews = home / "globalStorage" / "emptyWindowChatSessions"
        ews.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000002"
        session_id = f"local:copilot-vscode:{sid}"
        doc = json.dumps(
            {"sessionId": sid, "requests": [_request("r1")]}, indent=2
        ).encode()
        (ews / f"{sid}.json").write_bytes(doc)

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        found = source.discover_sessions()

        assert len(found) == 1
        assert source.get_agent_type(session_id) == "copilot-vscode"
        spans = source.get_spans_by_session(session_id)
        assert any(s.name == "Turn 1" for s in spans)

    def test_a_large_json_document_past_the_default_line_cap_is_not_truncated(
        self, tmp_path: Path
    ):
        # Regression: `_read_head` without `stop_when` stops at 20 LINES, but
        # only takes effect past the first 65536-byte chunk (the first chunk
        # is always read whole, so a small fixture hides the bug entirely --
        # caught in review of this very test: an earlier ~4KB/40-request
        # version of this fixture passed identically with and without the
        # fix, proving nothing). This fixture is deliberately padded well
        # past 65536 bytes so it spans multiple chunk reads, which is where
        # the early stop actually bites.
        home = tmp_path / "Code" / "User"
        ews = home / "globalStorage" / "emptyWindowChatSessions"
        ews.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000003"
        session_id = f"local:copilot-vscode:{sid}"
        n = 400
        requests = [
            {**_request(f"r{i}", ts_ms=i), "padding": "x" * 150} for i in range(n)
        ]
        doc = json.dumps({"sessionId": sid, "requests": requests}, indent=2).encode()
        assert len(doc) > 100_000, f"fixture must span multiple 65536B chunks, got {len(doc)}B"
        assert doc.count(b"\n") > 20, "fixture must exceed the 20-line default cap too"
        (ews / f"{sid}.json").write_bytes(doc)

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        found = source.discover_sessions()

        assert len(found) == 1, "a truncated document fails json.loads and drops the session"
        spans = source.get_spans_by_session(session_id)
        turns = [s for s in spans if s.name.startswith("Turn")]
        assert len(turns) == n, f"expected all {n} requests parsed, got {len(turns)}"

    def test_the_larger_jsonl_sibling_wins_over_a_smaller_json_even_when_older(
        self, tmp_path: Path
    ):
        # No bespoke "prefer jsonl" rule -- this relies on the SAME
        # size-first collision resolution every other agent's multi-file
        # case already uses (`_index_put`: byte size is the "more complete"
        # signal since transcripts are append-only; mtime only breaks an
        # EXACT size tie, per that function's own docstring).
        #
        # To prove SIZE, not recency, decides: the smaller .json is written
        # SECOND (so it has the newer mtime) and must still lose.
        home = tmp_path / "Code" / "User"
        ews = home / "globalStorage" / "emptyWindowChatSessions"
        ws = home / "workspaceStorage" / "abc123" / "chatSessions"
        ews.mkdir(parents=True)
        ws.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000004"
        session_id = f"local:copilot-vscode:{sid}"
        jsonl_bytes = _snapshot_line(sid, [_request("r1")])
        json_bytes = json.dumps({"sessionId": sid, "requests": []}, indent=2).encode()
        assert len(jsonl_bytes) > len(json_bytes), "fixture must genuinely differ in size"

        (ws / f"{sid}.jsonl").write_bytes(jsonl_bytes)  # larger, written (and so aged) first
        (ews / f"{sid}.json").write_bytes(json_bytes)  # smaller, written second -> newer mtime

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        found = source.discover_sessions()

        assert len(found) == 1, "same sessionId from two files must collapse to one session"
        spans = source.get_spans_by_session(session_id)
        # The larger .jsonl (with a real request) won despite being older.
        assert any(s.name == "Turn 1" for s in spans)

    def test_the_newer_file_wins_a_collision_only_when_sizes_are_exactly_equal(
        self, tmp_path: Path
    ):
        # Complement to the size-wins test above: `_index_put` compares
        # `(size, mtime)` as a tuple, so mtime is only ever consulted once
        # size is tied. Equal-size, distinct-content candidates isolate that
        # branch -- unequal sizes (as above) would pass even with a broken
        # mtime tie-break.
        home = tmp_path / "Code" / "User"
        ws = home / "workspaceStorage" / "abc123" / "chatSessions"
        ws.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000006"
        newer = _snapshot_line(sid, [_request("r1"), _request("r2")])
        older = _snapshot_line_padded(sid, [_request("r1")], target_len=len(newer))
        assert len(older) == len(newer), "fixture must be an exact size tie"

        older_path = ws / f"{sid}-a.jsonl"
        newer_path = ws / f"{sid}-b.jsonl"
        older_path.write_bytes(older)
        newer_path.write_bytes(newer)
        now = older_path.stat().st_mtime
        os.utime(older_path, (now - 60, now - 60))
        os.utime(newer_path, (now, now))

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        found = source.discover_sessions()

        assert len(found) == 1, "same sessionId from two files must collapse to one session"
        session_id = f"local:copilot-vscode:{sid}"
        spans = source.get_spans_by_session(session_id)
        turns = [s for s in spans if s.name.startswith("Turn")]
        assert len(turns) == 2, "the newer, equal-sized file (2 requests) must win the tie"

    def test_a_stray_non_matching_file_is_skipped_not_misindexed(self, tmp_path: Path):
        home = tmp_path / "Code" / "User"
        ws = home / "workspaceStorage" / "abc123" / "chatSessions"
        ws.mkdir(parents=True)
        (ws / "not-a-session.jsonl").write_bytes(b'{"unrelated": "shape"}\n')

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        found = source.discover_sessions()

        assert found == []
        assert source.discovery_stats()["copilot-vscode"]["skipped"].get("detection_failed") == 1

    def test_an_empty_file_is_skipped_and_labelled(self, tmp_path: Path):
        home = tmp_path / "Code" / "User"
        ews = home / "globalStorage" / "emptyWindowChatSessions"
        ews.mkdir(parents=True)
        (ews / "empty.json").touch()

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        source.discover_sessions()

        assert (
            source.discovery_stats()["copilot-vscode"]["skipped"].get("empty_or_unreadable") == 1
        )

    def test_a_final_state_json_document_with_zero_requests_is_skipped(self, tmp_path: Path):
        # Review-caught: distinct from the zero-byte case above. This is a
        # VALID, well-formed document (a real VS Code "empty window" session
        # abandoned before any message) -- the parser always emits zero spans
        # for it, which would otherwise be recorded as a
        # FAILED session on every repeated scan, not a benign empty one.
        home = tmp_path / "Code" / "User"
        ews = home / "globalStorage" / "emptyWindowChatSessions"
        ews.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000005"
        (ews / f"{sid}.json").write_bytes(
            json.dumps({"sessionId": sid, "requests": []}, indent=2).encode()
        )

        source = LocalSource(claude_code_homes=[], codex_homes=[], copilot_vscode_homes=[home])
        found = source.discover_sessions()

        assert found == []
        assert source.discovery_stats()["copilot-vscode"]["skipped"].get("no_conversation") == 1

    def test_no_homes_configured_is_a_no_op_not_an_error(self, tmp_path: Path):
        source = LocalSource(claude_code_homes=[], codex_homes=[])
        assert source.discover_sessions() == []
        assert source.copilot_vscode_homes == []

    def test_a_missing_home_directory_is_skipped_gracefully(self, tmp_path: Path):
        source = LocalSource(
            claude_code_homes=[],
            codex_homes=[],
            copilot_vscode_homes=[tmp_path / "does-not-exist"],
        )
        assert source.discover_sessions() == []
