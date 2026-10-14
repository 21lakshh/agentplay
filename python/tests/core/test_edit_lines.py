"""Unit tests for `ingestion/coding_agent/edit_lines.py` — AI lines of code.

Two properties matter and are pinned here: an `Edit` in context charges only
the lines it actually changed (not the whole surrounding block), and a session
that never produced a countable payload stays unmeasured (None) rather than
reporting 0.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from agentplay._core.ingestion.coding_agent.edit_lines import (
    count_patch_lines,
    edit_line_counts,
)
from agentplay._core.ingestion.coding_agent.session_pure import (
    edit_line_totals,
    session_derived_metrics,
)
from agentplay._core.ingestion.coding_agent.spans import SpanData


def _ts(offset_min: int = 0) -> datetime:
    return datetime(2026, 5, 30, 10, 0, 0, tzinfo=UTC) + timedelta(minutes=offset_min)


def _tool(name: str, payload: dict, *, ok: bool = True, at: int = 0) -> SpanData:
    return SpanData(
        span_id=f"trace:tool:{name}:{at}",
        trace_id="trace",
        parent_id="trace:turn:1",
        name=name,
        span_kind="TOOL",
        status_code="OK" if ok else "ERROR",
        start_time=_ts(at),
        end_time=_ts(at),
        attributes={"tool.name": name, "input.value": json.dumps(payload)},
    )


# ── per-call counting ───────────────────────────────────────────────


def test_edit_charges_only_the_changed_line() -> None:
    """A one-line change inside a block is 1 added / 1 removed, not 3 each.

    This is the whole reason the module runs a real diff instead of measuring
    both strings — charging the surrounding context would inflate every session
    that edits in place.
    """
    counts = edit_line_counts(
        "Edit",
        json.dumps(
            {
                "file_path": "/a/b.py",
                "old_string": "def f():\n    return 1\n",
                "new_string": "def f():\n    return 2\n",
            }
        ),
    )
    assert counts == (1, 1)


def test_pure_insertion_removes_nothing() -> None:
    counts = edit_line_counts(
        "Edit",
        json.dumps({"old_string": "a\nb\n", "new_string": "a\nNEW\nb\n"}),
    )
    assert counts == (1, 0)


def test_write_counts_whole_body_as_added() -> None:
    """`Write` carries no prior text, so its body is all additions."""
    counts = edit_line_counts("Write", json.dumps({"content": "one\ntwo\nthree"}))
    assert counts == (3, 0)


def test_trailing_newline_does_not_invent_a_line() -> None:
    assert edit_line_counts("Write", json.dumps({"content": "only\n"})) == (1, 0)


def test_multiedit_sums_its_edits() -> None:
    counts = edit_line_counts(
        "MultiEdit",
        json.dumps(
            {
                "file_path": "/a/b.py",
                "edits": [
                    {"old_string": "x\n", "new_string": "y\n"},
                    {"old_string": "", "new_string": "added\n"},
                ],
            }
        ),
    )
    assert counts == (2, 1)


def test_unhashable_tool_name_returns_none_not_typeerror() -> None:
    """`tool.name` is a span attribute, so a malformed transcript can put a list
    or dict there. `in` on an unhashable raises, and `edit_line_totals` has no
    per-call guard — one malformed tool would abort the whole session's counts.
    """
    assert edit_line_counts(["Edit"], '{"new_string": "x"}') is None
    assert edit_line_counts({"tool": "Edit"}, '{"new_string": "x"}') is None
    assert edit_line_counts(None, '{"new_string": "x"}') is None


def test_uncountable_inputs_are_none_not_zero() -> None:
    """Unknown must never collapse to a confident 0."""
    assert edit_line_counts("Bash", json.dumps({"command": "sed -i s/a/b/ f.py"})) is None
    assert edit_line_counts("Read", json.dumps({"file_path": "/a/b.py"})) is None
    assert edit_line_counts("Edit", "not json at all") is None
    assert edit_line_counts("Edit", json.dumps({"file_path": "/a/b.py"})) is None
    assert edit_line_counts("Edit", "") is None
    assert edit_line_counts("Edit", None) is None


# ── codex apply_patch ───────────────────────────────────────────────


def test_count_patch_lines_counts_hunk_content_only() -> None:
    body = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@ -1,3 +1,3 @@\n"
        " context\n"
        "-old line\n"
        "+new line\n"
        "+extra line\n"
        "*** End Patch\n"
    )
    assert count_patch_lines(body) == (2, 1)


def test_count_patch_lines_counts_double_prefixed_content() -> None:
    """apply_patch has no `+++` / `---` file markers to skip.

    A source line beginning `++` is encoded `+++`, so treating those as unified-
    diff markers silently undercounted every such added line.
    """
    body = "*** Update File: a.cpp\n+++i;\n+plain\n---j;\n"
    assert count_patch_lines(body) == (2, 1)


def test_stamped_counts_survive_truncation() -> None:
    """The whole point of stamping at parse time.

    Codex apply_patch spans are often cut
    mid-string by the parser's 2,000-char cap, so `json.loads` fails. The stamp
    sits in the retained prefix and is read by regex instead.
    """
    truncated = (
        '{"file_path": "a.py", "lines_added": 42, "lines_removed": 7, '
        '"patch": "*** Begin Patch\\n+one\\n+tw'
    )
    with pytest.raises(json.JSONDecodeError):
        json.loads(truncated)
    assert edit_line_counts("Edit", truncated) == (42, 7)


def test_stamped_counts_win_over_the_payload_body() -> None:
    """A stamp was computed before truncation, so it is the better source."""
    stamped = (
        '{"file_path": "a.py", "lines_added": 99, "lines_removed": 1, '
        '"patch": "*** Begin Patch\\n+only-one-line\\n*** End Patch\\n"}'
    )
    assert edit_line_counts("Edit", stamped) == (99, 1)


def test_unstamped_patch_payload_is_still_unmeasured() -> None:
    """An older parser emitted no stamp and no old_string / new_string."""
    complete = json.dumps({"file_path": "a.py", "patch": "*** Begin Patch\n+x\n"})
    assert edit_line_counts("Edit", complete) is None


# ── session fold ────────────────────────────────────────────────────


def test_session_totals_sum_successful_edits() -> None:
    calls = [
        _tool("Write", {"content": "a\nb\n"}, at=0),
        _tool("Edit", {"old_string": "a\n", "new_string": "z\n"}, at=1),
    ]
    from agentplay._core.ingestion.coding_agent.session_pure import _tool_calls

    assert edit_line_totals(_tool_calls(calls)) == (3, 1)


def test_failed_edit_changed_no_lines() -> None:
    from agentplay._core.ingestion.coding_agent.session_pure import _tool_calls

    calls = _tool_calls([_tool("Edit", {"old_string": "a\n", "new_string": "z\n"}, ok=False)])
    assert edit_line_totals(calls) == (None, None)


def test_session_with_no_edits_is_unmeasured() -> None:
    from agentplay._core.ingestion.coding_agent.session_pure import _tool_calls

    calls = _tool_calls([_tool("Read", {"file_path": "/a/b.py"})])
    assert edit_line_totals(calls) == (None, None)


# ── output keys ──────────────────────────────────────────────────────


def test_keys_reach_the_wire_when_measured() -> None:
    spans = [_tool("Write", {"content": "a\nb\nc\n"})]
    out = session_derived_metrics(spans)
    assert out["lines_added"] == 3
    assert out["lines_removed"] == 0


def test_keys_omitted_when_unmeasured() -> None:
    """Omitted, not null-valued, so a consumer can show "unmeasured" instead of 0."""
    out = session_derived_metrics([_tool("Read", {"file_path": "/a/b.py"})])
    assert "lines_added" not in out
    assert "lines_removed" not in out
