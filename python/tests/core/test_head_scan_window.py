"""Head-scanning must read every line `_read_head` returned, not the first 20.

# Why this exists

`_read_head` returns complete lines rather than a byte slab, so a giant opening
record is never cut mid-JSON. It returns *at least* 20 complete lines and
usually more (it reads 64KB chunks), so callers must scan all of them rather
than slicing `head.split(b"\n")[:20]` and dropping the remainder.

Some transcripts open with 23-67 lines of
metadata (`ai-title`, `agent-name`, `mode`, `permission-mode`,
`file-history-snapshot`, `cursor_usage_event`) before the first real record.
Discarding the head above the cap would drop those sessions, including a
`cursor-ide` transcript whose `cursor_usage_event` stream is the only place its
billed tokens live.

A transcript is not malformed because it front-loads metadata.
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.base import detect_agent


def _claude_transcript(metadata_lines: int) -> bytes:
    """A valid Claude Code transcript preceded by `metadata_lines` of preamble."""
    kinds = ["ai-title", "agent-name", "mode", "permission-mode", "file-history-snapshot"]
    head = [json.dumps({"type": kinds[i % len(kinds)]}) for i in range(metadata_lines)]
    head.append(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "hi"},
                "sessionId": "s",
                "uuid": "u",
                "timestamp": "2026-07-31T00:00:00Z",
            }
        )
    )
    return "\n".join(head).encode()


def test_detects_when_the_first_record_is_within_the_old_window() -> None:
    """The case that always worked — must keep working."""
    assert detect_agent(_claude_transcript(5)) is not None


def test_detects_when_the_first_record_sits_just_past_line_20() -> None:
    """23-28 metadata lines push the first `user` record past line 20; a valid
    transcript must still be detected rather than skipped."""
    assert detect_agent(_claude_transcript(23)) is not None
    assert detect_agent(_claude_transcript(28)) is not None


def test_detects_a_deeply_front_loaded_transcript() -> None:
    """The worst observed opener ran to 67 metadata lines (a cursor-ide file)."""
    assert detect_agent(_claude_transcript(67)) is not None


def test_still_returns_none_for_a_file_with_no_agent_marker() -> None:
    """Widening the scan must not make detection credulous — an unrecognised
    file still has to be skipped rather than guessed at."""
    junk = b"\n".join(json.dumps({"type": "ai-title"}).encode() for _ in range(40))
    assert detect_agent(junk) is None


def test_empty_input_is_none() -> None:
    assert detect_agent(b"") is None


# --- The READ axis -----------------------------------------------------------
# A third failure mode. The byte axis was fixed (complete lines, not a
# 32KB slab) and the line axis was fixed (scan every line the head returned).
# What stayed broken is how much `_read_head` *reads*: it stopped as soon as it
# held `min_lines` complete lines, so a transcript whose identifying record sits
# past that point never had it in the buffer at all — the scan above cannot find
# what was never read.
#
# Observed: a large real transcript opens with 39 metadata records, its first
# `user` line is #40, and a single 64KB chunk already satisfied 20 lines at line
# 25. `detect_agent` returned None and that session was skipped on EVERY scan,
# permanently. Detection is why the head is read, so detection is what has to
# decide when to stop.


def _write(tmp_path, name: str, payload: bytes):
    p = tmp_path / name
    p.write_bytes(payload)
    return p


def test_reads_past_min_lines_until_the_agent_marker_is_found(tmp_path) -> None:
    """The regression: 39 metadata records, identifying record at line 40."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import (
        _agent_detected,
        _read_head,
    )

    # Pad each metadata line so 20 lines comfortably exceed one 64KB chunk,
    # reproducing "chunk satisfied min_lines before the marker was reached".
    filler = "x" * 4000
    lines = [json.dumps({"type": "queue-operation", "pad": filler}) for _ in range(39)]
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "hi"},
                "sessionId": "s",
                "uuid": "u",
                "timestamp": "2026-08-13T00:00:00Z",
            }
        )
    )
    f = _write(tmp_path, "long_preamble.jsonl", "\n".join(lines).encode())

    assert detect_agent(_read_head(f)) is None, "precondition: the old stop rule misses it"
    assert detect_agent(_read_head(f, stop_when=_agent_detected)) is not None


def test_common_case_still_stops_at_the_first_chunk(tmp_path) -> None:
    """The predicate must not turn every scan into a full-file read."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import (
        _agent_detected,
        _read_head,
    )

    body = [
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "hi"},
                "sessionId": "s",
                "uuid": f"u{i}",
                "timestamp": "2026-08-13T00:00:00Z",
            }
        )
        for i in range(4000)
    ]
    f = _write(tmp_path, "normal.jsonl", "\n".join(body).encode())

    head = _read_head(f, stop_when=_agent_detected)
    assert detect_agent(head) is not None
    assert len(head) <= 65536, "a normal transcript must still cost one chunk"


def test_unrecognisable_file_is_bounded_by_max_bytes(tmp_path) -> None:
    """No marker ever appears — the read must stop, not run to EOF unbounded."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import (
        _agent_detected,
        _read_head,
    )

    junk = "\n".join(json.dumps({"type": "nope", "i": i}) for i in range(50_000))
    f = _write(tmp_path, "junk.jsonl", junk.encode())

    head = _read_head(f, stop_when=_agent_detected, max_bytes=200_000)
    assert detect_agent(head) is None
    assert len(head) <= 200_000 + 65536


def test_predicate_sees_each_line_once_not_the_whole_buffer(tmp_path) -> None:
    """Guards the O(n^2) trap.

    Handing `stop_when` the whole accumulated buffer every chunk made an
    unrecognised 5.9MB JSONL cost 1.3s instead of 0.1ms. Asserting on total
    bytes handed to the predicate rather than wall-clock keeps this
    deterministic: linear means ~one pass over the file, quadratic means
    hundreds.
    """
    from agentplay._core.ingestion.coding_agent._sources.local.source import _read_head

    junk = "\n".join(json.dumps({"type": "nope", "i": i, "pad": "x" * 80}) for i in range(20_000))
    f = _write(tmp_path, "junk.jsonl", junk.encode())
    size = f.stat().st_size

    seen = []

    def _never(buf: bytes) -> bool:
        seen.append(len(buf))
        return False

    _read_head(f, stop_when=_never, max_bytes=size + 1)

    # Every complete line offered exactly once => total <= file size.
    assert sum(seen) <= size, f"predicate re-read the buffer: {sum(seen):,} > {size:,}"


def test_predicate_segments_do_not_overlap(tmp_path) -> None:
    """Each segment must start where the previous ended, on a line boundary."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import _read_head

    body = "\n".join(json.dumps({"type": "meta", "i": i, "pad": "z" * 500}) for i in range(2_000))
    f = _write(tmp_path, "segments.jsonl", body.encode())

    chunks = []

    def _collect(buf: bytes) -> bool:
        chunks.append(buf)
        return False

    _read_head(f, stop_when=_collect, max_bytes=f.stat().st_size + 1)

    rejoined = b"".join(chunks)
    assert rejoined == f.read_bytes()[: len(rejoined)], "segments must tile the file in order"
    for c in chunks:
        assert c.endswith(b"\n"), "every segment must end on a line boundary"


def test_metadata_is_found_on_a_late_first_record(tmp_path) -> None:
    """Fourth cap, same failure.

    Rescuing a long-preamble session from the drop is only half the job: `cwd`
    and `gitBranch` live on the SAME late record that detection was looking for,
    so a 30-line cap in `_first_field_in_cc_head` returned None for both and the
    session landed with no repo attribution — silently degraded, which is worse
    than the drop it replaced.
    """
    from agentplay._core.ingestion.coding_agent._sources.local.source import (
        _branch_from_cc_head,
        _cwd_from_cc_head,
    )

    lines = [json.dumps({"type": "queue-operation", "i": i}) for i in range(39)]
    lines.append(
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "hi"},
                "sessionId": "s",
                "uuid": "u",
                "timestamp": "2026-08-13T00:00:00Z",
                "cwd": "/Users/dev/repo",
                "gitBranch": "feat/x",
            }
        )
    )
    head = "\n".join(lines).encode()

    assert _cwd_from_cc_head(head) == "/Users/dev/repo"
    assert _branch_from_cc_head(head) == "feat/x"


def test_metadata_on_the_first_record_still_returns_immediately(tmp_path) -> None:
    """The common case must not pay for the uncapped scan."""
    from agentplay._core.ingestion.coding_agent._sources.local.source import _cwd_from_cc_head

    first = json.dumps(
        {"type": "user", "sessionId": "s", "uuid": "u", "cwd": "/repo", "gitBranch": "main"}
    )
    tail = "\n".join(json.dumps({"type": "user", "i": i}) for i in range(5_000))
    assert _cwd_from_cc_head(f"{first}\n{tail}".encode()) == "/repo"


def test_no_complete_line_means_the_predicate_is_never_called(tmp_path) -> None:
    """A buffer holding no complete line must not be handed to `stop_when`.

    Scope note, because the obvious stronger claim does not hold: this does NOT
    prove the line-boundary search is O(chunk). The earlier whole-buffer `rfind`
    also returned -1 here and also skipped the call, so this assertion passes on
    both implementations. The O(chunk) property comes from searching the chunk
    rather than the buffer (`chunk.rfind` + the chunk's start offset) and is
    structural, not asserted here — a pure cost property needs either timing,
    which is flaky, or instrumentation the function does not expose.

    What this does pin: partial records are never parsed.
    """
    from agentplay._core.ingestion.coding_agent._sources.local.source import _read_head

    f = _write(tmp_path, "single.jsonl", (json.dumps({"type": "x", "pad": "y" * 900_000})).encode())

    calls = []

    def _never(buf: bytes) -> bool:
        calls.append(len(buf))
        return False

    _read_head(f, stop_when=_never, max_bytes=f.stat().st_size + 1)

    assert calls == [], "a buffer with no complete line must not be scanned"


def test_line_boundary_is_tracked_across_a_giant_opener(tmp_path) -> None:
    """A giant first line followed by real records still finds the marker.

    The fixture ends with a trailing newline on purpose: without it the marker
    record is not a COMPLETE line, so `stop_when` never receives it and
    `detect_agent` would only see it in the EOF buffer `_read_head` returns
    anyway — the test would pass even if predicate-based stopping were broken.
    """
    from agentplay._core.ingestion.coding_agent._sources.local.source import (
        _agent_detected,
        _cwd_from_cc_head,
        _read_head,
    )

    lines = [json.dumps({"type": "queue-operation", "pad": "y" * 300_000})]
    lines += [json.dumps({"type": "meta", "i": i}) for i in range(30)]
    lines.append(
        json.dumps({"type": "user", "sessionId": "s", "uuid": "u", "cwd": "/repo"})
    )
    f = _write(tmp_path, "mixed.jsonl", ("\n".join(lines) + "\n").encode())

    # The predicate itself must be satisfied, not just the returned buffer.
    seen = []

    def _spy(buf: bytes) -> bool:
        seen.append(buf)
        return _agent_detected(buf)

    head = _read_head(f, stop_when=_spy)
    assert any(_agent_detected(b) for b in seen), "the marker must reach stop_when"
    assert detect_agent(head) is not None
    assert _cwd_from_cc_head(head) == "/repo"
