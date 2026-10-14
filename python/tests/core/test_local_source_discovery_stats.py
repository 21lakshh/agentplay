"""Per-agent file-discovery counts (seen / indexed / skipped).

`LocalSource._build_index()` already visits every `.jsonl` under every
configured home once; these tests pin that walk now also tallies, per agent
type, how many files it saw, how many became a real session, and — when it
skipped one — *why*. `discovery_stats()` must never expose more than an
integer count: no file path, name, or byte size is ever attached to the
tally, matching this codebase's identity-stripping discipline for anything
that could reach a diagnostic event or the heartbeat.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentplay._core.ingestion.coding_agent._sources.local.source import (
    LocalSource,
)


def _jsonl(records: list[dict]) -> bytes:
    return b"\n".join(json.dumps(r).encode() for r in records) + b"\n"


def _cc_user(session_id: str, ts: str = "2026-09-01T00:00:00Z") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "sessionId": session_id,
        "cwd": "/repo",
        "message": {"role": "user", "content": "hi"},
    }


def _cc_assistant(session_id: str, message_id: str, ts: str = "2026-09-01T00:00:01Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": session_id,
        "cwd": "/repo",
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi back"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


class TestClaudeCodeDiscoveryStats:
    def test_a_real_session_counts_as_seen_and_indexed(self, tmp_path: Path):
        home = tmp_path / ".claude"
        slug = home / "projects" / "-repo"
        slug.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000001"
        (slug / f"{sid}.jsonl").write_bytes(_jsonl([_cc_user(sid), _cc_assistant(sid, "m1")]))

        source = LocalSource(claude_code_homes=[home], codex_homes=[])
        found = source.discover_sessions()

        assert len(found) == 1
        assert source.discovery_stats() == {"claude-code": {"seen": 1, "indexed": 1, "skipped": {}}}

    def test_a_subagent_component_file_is_skipped_and_labelled_as_such(self, tmp_path: Path):
        home = tmp_path / ".claude"
        slug = home / "projects" / "-repo"
        sid = "9f1c3c2a-0000-4000-8000-000000000002"
        (slug / sid / "subagents").mkdir(parents=True)
        (slug / f"{sid}.jsonl").write_bytes(_jsonl([_cc_user(sid), _cc_assistant(sid, "m1")]))
        # A subagent's own file: every line isSidechain=true.
        side = {**_cc_assistant(sid, "m-side"), "isSidechain": True}
        (slug / sid / "subagents" / "agent-a1.jsonl").write_bytes(_jsonl([side]))

        source = LocalSource(claude_code_homes=[home], codex_homes=[])
        source.discover_sessions()

        stats = source.discovery_stats()["claude-code"]
        assert stats["seen"] == 2
        assert stats["indexed"] == 1
        assert stats["skipped"] == {"subagent_component": 1}

    def test_undetectable_shape_is_a_detection_failure_not_silently_ignored(self, tmp_path: Path):
        home = tmp_path / ".claude"
        slug = home / "projects" / "-repo"
        slug.mkdir(parents=True)
        # Valid JSON, but no agent claims this shape.
        (slug / "mystery.jsonl").write_bytes(_jsonl([{"type": "workflow_journal", "note": "x"}]))

        source = LocalSource(claude_code_homes=[home], codex_homes=[])
        source.discover_sessions()

        assert source.discovery_stats()["claude-code"] == {
            "seen": 1,
            "indexed": 0,
            "skipped": {"detection_failed": 1},
        }

    def test_a_different_agents_file_in_the_same_home_is_not_a_failure(self, tmp_path: Path):
        home = tmp_path / ".claude"
        slug = home / "projects" / "-repo"
        slug.mkdir(parents=True)
        # A Codex file that happens to live under a Claude Code home.
        (slug / "codex.jsonl").write_bytes(
            _jsonl([{"type": "session_meta", "payload": {"originator": "codex_cli_rs"}}])
        )

        source = LocalSource(claude_code_homes=[home], codex_homes=[])
        source.discover_sessions()

        assert source.discovery_stats()["claude-code"] == {
            "seen": 1,
            "indexed": 0,
            "skipped": {"different_agent": 1},
        }

    def test_empty_file_is_a_distinct_reason_from_detection_failure(self, tmp_path: Path):
        home = tmp_path / ".claude"
        slug = home / "projects" / "-repo"
        slug.mkdir(parents=True)
        (slug / "empty.jsonl").write_bytes(b"")

        source = LocalSource(claude_code_homes=[home], codex_homes=[])
        source.discover_sessions()

        assert source.discovery_stats()["claude-code"] == {
            "seen": 1,
            "indexed": 0,
            "skipped": {"empty_or_unreadable": 1},
        }

    def test_a_directory_matching_the_glob_is_never_tallied_as_a_phantom_file(self, tmp_path: Path):
        """`rglob("*.jsonl")` can yield a directory (or a
        broken symlink) with that suffix; it must be silently skipped,
        never counted as a seen/empty file -
        that would report a phantom transcript on the heartbeat."""
        home = tmp_path / ".claude"
        slug = home / "projects" / "-repo"
        slug.mkdir(parents=True)
        (slug / "not-a-file.jsonl").mkdir()  # a directory, not a transcript
        sid = "9f1c3c2a-0000-4000-8000-000000000004"
        (slug / f"{sid}.jsonl").write_bytes(_jsonl([_cc_user(sid), _cc_assistant(sid, "m1")]))

        source = LocalSource(claude_code_homes=[home], codex_homes=[])
        source.discover_sessions()

        # Only the real transcript is tallied; the directory is invisible to
        # the tally entirely, not counted as "seen" or as any skip reason.
        assert source.discovery_stats()["claude-code"] == {
            "seen": 1,
            "indexed": 1,
            "skipped": {},
        }

    def test_stats_never_carry_a_file_path_name_or_size(self, tmp_path: Path):
        """The whole point of the tally: aggregate counts only, nothing per-file."""
        home = tmp_path / ".claude"
        slug = home / "projects" / "-a-very-identifying-project-name"
        slug.mkdir(parents=True)
        (slug / "some-session-id.jsonl").write_bytes(_jsonl([{"type": "unknown"}]))

        source = LocalSource(claude_code_homes=[home], codex_homes=[])
        source.discover_sessions()

        dumped = json.dumps(source.discovery_stats())
        assert "a-very-identifying-project-name" not in dumped
        assert "some-session-id" not in dumped
        assert str(home) not in dumped


class TestCrossAgentAggregation:
    def test_counts_land_under_separate_top_level_keys_per_agent(self, tmp_path: Path):
        cc_home = tmp_path / ".claude"
        cc_slug = cc_home / "projects" / "-repo"
        cc_slug.mkdir(parents=True)
        sid = "9f1c3c2a-0000-4000-8000-000000000003"
        (cc_slug / f"{sid}.jsonl").write_bytes(_jsonl([_cc_user(sid), _cc_assistant(sid, "m1")]))

        cursor_home = tmp_path / ".cursor-store"
        cursor_home.mkdir(parents=True)
        (cursor_home / "sess.jsonl").write_bytes(
            _jsonl(
                [
                    {"type": "cursor_store_session", "cwd": "/repo"},
                    {"role": "user", "content": "hi"},
                ]
            )
        )

        source = LocalSource(
            claude_code_homes=[cc_home], codex_homes=[], cursor_store_homes=[cursor_home]
        )
        source.discover_sessions()

        stats = source.discovery_stats()
        assert stats["claude-code"]["indexed"] == 1
        assert stats["cursor"]["indexed"] == 1
        # Each agent's tally is independent — one agent's file never inflates
        # or shrinks another agent's counts just because they share a run.
        assert stats["claude-code"]["seen"] == 1
        assert stats["cursor"]["seen"] == 1


class TestCursorDiscoveryStats:
    def test_a_subagent_file_under_a_cursor_home_is_skipped_and_labelled(self, tmp_path: Path):
        home = tmp_path / ".cursor"
        parent_dir = home / "projects" / "-repo" / "agent-transcripts" / "conv1"
        (parent_dir / "subagents").mkdir(parents=True)
        (parent_dir / "conv1.jsonl").write_bytes(
            _jsonl([{"role": "user", "message": {"content": "hi"}}])
        )
        (parent_dir / "subagents" / "agent-a1.jsonl").write_bytes(
            _jsonl([{"role": "assistant", "message": {"content": "sub"}}])
        )

        source = LocalSource(claude_code_homes=[], codex_homes=[], cursor_store_homes=[home])
        source.discover_sessions()

        stats = source.discovery_stats()["cursor"]
        assert stats["seen"] == 2
        assert stats["indexed"] == 1
        assert stats["skipped"] == {"subagent_component": 1}

    def test_a_store_session_with_no_messages_is_its_own_reason(self, tmp_path: Path):
        home = tmp_path / ".cursor-store"
        home.mkdir(parents=True)
        (home / "empty-sess.jsonl").write_bytes(
            _jsonl([{"type": "cursor_store_session", "cwd": "/repo"}])
        )

        source = LocalSource(claude_code_homes=[], codex_homes=[], cursor_store_homes=[home])
        source.discover_sessions()

        assert source.discovery_stats()["cursor"] == {
            "seen": 1,
            "indexed": 0,
            "skipped": {"empty_store_session": 1},
        }
