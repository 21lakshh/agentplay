"""Added / removed line counts from an edit tool span's `input.value`.

The AI-lines-of-code signal. Counted locally, for two reasons.

First, lines added is a plain count. Ratios built on top of it, such as an
AI-code percentage, need totals this module cannot see and are derived by the
consumer, in line with `metrics.py`'s "Counts only; ratios are derived by the
consumer".

Second, and load-bearing: the raw material never leaves the machine. A tool
span's `input.value` holds the actual edit payload, and the session payload
deliberately excludes tool arguments and results because "those are
content". Counting locally and emitting two integers keeps that property. Any
downstream derivation would mean shipping diffs.

Deliberately narrow, in the same spirit as `file_paths.py`: only the four tools
whose input genuinely carries the changed text, and a strict `json.loads` rather
than a regex. A payload that does not parse yields None (unknown), never 0 —
persisting a fabricated 0 would launder an unmeasured session into a real-looking
measurement, the same distinction `session_pure.interrupt_count_measured`
documents.

Per-agent coverage is NOT uniform, and the gap is deliberate rather than hidden.
The local parsers do normalize every agent's edits into this tool taxonomy, so
this module never branches per agent — but what reaches `input.value` differs:

- Claude Code: full `old_string` / `new_string` / `content`. Measured; the diff
  discounts unchanged context lines that a naive payload-line count would include.
- Codex: `apply_patch` arrives as `{"file_path": ..., "patch": <body>}`, and
  `parsers/codex.py` truncates `input.value` at 2,000 characters with the patch
  key LAST — so any non-trivial patch is cut mid-string and fails `json.loads`,
  yielding None. Counting only the patches small enough to survive would report
  a confident number biased low by exactly the large edits that matter most, so
  unmeasured is the honest answer until the parser stamps counts at parse time
  (where it still holds the untruncated body).
- Cursor / opencode / kiro: same 2,000-character truncation applies.

Note for future edits: `parsers/codex.py::_rewrite_apply_patch_input` documents
that its repeated `"file_path"` keys are safe because "no current consumer" uses
`json.loads`. This module is the first one that does. Duplicate keys resolve
last-wins, which is harmless here (the path is not read), but a consumer added
later that needs every path must stay regex-based per `file_paths.py`.
"""

from __future__ import annotations

import difflib
import json
import re

# Tools whose `input.value` carries the changed text. `Edit` and `MultiEdit`
# carry BOTH sides, so they diff exactly. `Write` and `NotebookEdit` carry only
# the new content — the prior text is not in the payload — so every line reads
# as added; see `_full_content_counts`.
_DIFF_TOOLS: frozenset[str] = frozenset({"Edit"})
_MULTI_EDIT_TOOLS: frozenset[str] = frozenset({"MultiEdit"})
_FULL_CONTENT_TOOLS: frozenset[str] = frozenset({"Write", "NotebookEdit"})

EDIT_LINE_TOOLS: frozenset[str] = _DIFF_TOOLS | _MULTI_EDIT_TOOLS | _FULL_CONTENT_TOOLS

# Above this combined line count a real diff stops being worth its runtime, so
# both sides are charged whole. Generated files and vendored blobs are the only
# realistic way to reach it; a hand-written edit never does.
_DIFF_LINE_CAP = 20_000

# Keys holding the post-edit text, in the order the tools use them.
_CONTENT_KEYS = ("content", "new_source", "new_string")

# Counts stamped by a parser that held the untruncated body (codex apply_patch).
# Matched positionally so a truncated tail cannot break the read.
_RE_STAMPED_COUNTS = re.compile(r'"lines_added":\s*(\d+),\s*"lines_removed":\s*(\d+)')


def _lines(text: object) -> list[str]:
    """Line list for a payload field, tolerant of a missing / non-string value.

    Uses `splitlines()` so a trailing newline does not invent an empty final
    line — `"a\\n"` is one line, not two.
    """
    return text.splitlines() if isinstance(text, str) and text else []


def _diff_counts(old: object, new: object) -> tuple[int, int]:
    """`(added, removed)` between two versions of a file's text.

    A real line diff rather than `len(new), len(old)`: an `Edit` that rewrites
    one line inside a ten-line block changed one line, and charging it ten each
    way would inflate every session that edits in context.
    """
    old_lines, new_lines = _lines(old), _lines(new)
    if len(old_lines) + len(new_lines) > _DIFF_LINE_CAP:
        return len(new_lines), len(old_lines)
    added = removed = 0
    # autojunk=False: its "popular element" heuristic treats lines recurring in
    # >1% of a long sequence as junk, which in source files means blank lines and
    # closing braces — exactly the lines that anchor a correct diff.
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            added += j2 - j1
        if tag in ("replace", "delete"):
            removed += i2 - i1
    return added, removed


def _full_content_counts(payload: dict) -> tuple[int, int] | None:
    """`(added, 0)` for a tool that carries only the post-edit text.

    `Write` overwriting an existing file is a full rewrite whose prior content
    the payload does not carry, so its whole body counts as added and nothing as
    removed. That over-counts a rewrite relative to a diff — accepted, because
    the alternative is reading the file from disk at parse time, which the
    parsers deliberately never do.
    """
    for key in _CONTENT_KEYS:
        if isinstance(payload.get(key), str):
            return len(_lines(payload[key])), 0
    return None


def count_patch_lines(patch_text: object) -> tuple[int, int]:
    """`(added, removed)` for a codex `apply_patch` body.

    Called by `_sources/local/parsers/codex.py` at PARSE time, on the
    untruncated body. It has to happen there: the parser caps `input.value` at
    2,000 characters, so by the time a consumer sees the span the patch tail is
    gone: a typical apply_patch span is cut mid-string.

    Counts hunk content only. `+`/`-` lines are additions/removals; `***`
    headers, `@@` markers and context lines are not content. A body line that
    merely quotes a header (`+*** Update File: x`) is an added line and counts
    as one, which the prefix test gets right for free.

    Assumes the apply_patch grammar specifically, NOT unified diff: there are no
    `+++` / `---` file markers to skip, and treating them as markers would drop
    real content (a source line starting `++` arrives as `+++`).

    A `*** Delete File:` header carries no body, so the removed lines of the
    file it drops are invisible here and counted as 0 — the same limit `Write`
    has when it overwrites a file whose prior content the payload never carried.

    Args:
        patch_text: The raw `*** Begin Patch` body.

    Returns:
        `(added, removed)`; `(0, 0)` when the input is not a usable string.
    """
    if not isinstance(patch_text, str) or not patch_text:
        return 0, 0
    added = removed = 0
    for line in patch_text.splitlines():
        # No `+++` / `---` skip here. Those are UNIFIED-diff file markers, and
        # this grammar has none — apply_patch names files on `*** <Verb> File:`
        # headers. Skipping them would instead drop real content: a source line
        # beginning `++` is encoded `+++`, so such a guard would undercount
        # every added line that starts with two plus signs.
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def _stamped_counts(input_value: str) -> tuple[int, int] | None:
    """Counts a parser already stamped into `input.value`, if present.

    Regex rather than `json.loads` deliberately: a stamped payload whose tail
    was truncated is no longer parseable JSON, and the stamp exists precisely so
    those sessions stay measurable. This also keeps the module aligned with
    `file_paths.py`'s contract that `input.value` consumers are regex-based.
    """
    match = _RE_STAMPED_COUNTS.search(input_value)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def edit_line_counts(tool_name: object, input_value: object) -> tuple[int, int] | None:
    """Lines added and removed by one tool call.

    Args:
        tool_name: The span's `tool.name`. Anything outside `EDIT_LINE_TOOLS`
            returns None — a `Bash` `sed` call edits files too, but its payload
            carries a command rather than the text, so it is not countable here.
        input_value: The span's `input.value` attribute, expected to be a JSON
            object string.

    Returns:
        `(added, removed)`, or None when the tool is not a counted edit tool or
        the payload cannot be parsed into the fields this needs.
    """
    # `str` check before set membership: `tool.name` is a span attribute, so a
    # malformed transcript can put a list or dict there, and `in` on an
    # unhashable raises TypeError. `edit_line_totals` has no per-call guard, so
    # one malformed tool would abort the whole session's counts instead of the
    # documented None.
    if not isinstance(tool_name, str) or tool_name not in EDIT_LINE_TOOLS:
        return None
    if not isinstance(input_value, str) or not input_value:
        return None
    # A parser-stamped count wins: it was computed before truncation, so it is
    # both more accurate and readable on payloads json.loads would reject.
    stamped = _stamped_counts(input_value)
    if stamped is not None:
        return stamped
    try:
        payload = json.loads(input_value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    if tool_name in _MULTI_EDIT_TOOLS:
        edits = payload.get("edits")
        if not isinstance(edits, list):
            return None
        added = removed = 0
        counted = False
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            one_added, one_removed = _diff_counts(edit.get("old_string"), edit.get("new_string"))
            added += one_added
            removed += one_removed
            counted = True
        return (added, removed) if counted else None

    if tool_name in _DIFF_TOOLS:
        if "old_string" not in payload and "new_string" not in payload:
            return None
        return _diff_counts(payload.get("old_string"), payload.get("new_string"))

    return _full_content_counts(payload)


__all__ = ["EDIT_LINE_TOOLS", "count_patch_lines", "edit_line_counts"]
