"""Shared file-path extraction from a tool span's `input.value`.

`metrics.py` classifies tool calls by the file they touched (blind-edit,
duplicate-read, repeated-edit, single-file-focus, ...). Every local parser
JSON-serializes the touched file under a `"file_path":"..."` key by the
time its spans reach that consumer — Claude Code, Codex, and opencode use `file_path` natively;
Cursor's raw `"path"`/`"paths"` args are normalized to `file_path` by the
Cursor parser itself (`_sources/local/parsers/cursor_store.py::
_build_tool_span`) before the span is built. So this module stays a
single, narrow `file_path`-key regex match for every source — widening it
here (e.g. matching a bare `"path"` key or falling back to a whitespace
heuristic) would leak paths out of non-file spans too, like a Claude
`Bash` `{"command": "cat /a/b.py"}` or a `Grep` `{"path": "/a"}`, which
never carried a file path pre-Cursor-support and must not start now.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
import re

from agentplay._core.ingestion.coding_agent._paths import to_posix

_RE_FILE_PATH = re.compile(r'"file_path":\s*"([^"]+)"')


def extract_file_path(input_value: object) -> str | None:
    """Pull the `file_path` value out of a tool span's `input.value`.

    Args:
        input_value: The span's `input.value` attribute — expected to be
            a JSON-ish string; any other type (None, dict, ...) returns
            None.

    Returns:
        The first `"file_path":"..."` match, or None when `input_value`
        isn't a non-empty string or contains no match.
    """
    if not isinstance(input_value, str) or not input_value:
        return None
    m = _RE_FILE_PATH.search(input_value)
    return m.group(1) if m else None


def extract_all_file_paths(input_value: object) -> set[str]:
    """Every `"file_path":"..."` match in a tool span's `input.value`.

    Args:
        input_value: The span's `input.value` attribute.

    Returns:
        The set of all `file_path` matches, or an empty set when none
        are found (or `input_value` isn't a non-empty string).
    """
    if not isinstance(input_value, str) or not input_value:
        return set()
    return {m.group(1) for m in _RE_FILE_PATH.finditer(input_value)}


__all__ = ["extract_all_file_paths", "extract_file_path"]


def repo_relative_path(path: object, repo_name: str) -> str | None:
    """Normalize a recorded file against its already-attributed repository.

    Absolute paths need the repository's own leaf in their ancestry. Reject
    traversal and repeated leaf segments: neither identifies an exact root.
    Relative paths stay relative; this never searches other repositories.
    """
    if not isinstance(path, str) or not path or not repo_name:
        return None
    path = to_posix(path)
    parts = path.split("/")
    if ".." in parts or "\x00" in path:
        return None
    parts = [part for part in parts if part and part != "."]
    if path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        leaf = repo_name.rsplit("/", 1)[-1]
        # Use the recorded platform, not the host running a replay. Preserve
        # the original relative-path casing and POSIX case sensitivity.
        key = str.casefold if PureWindowsPath(path).is_absolute() else str
        matches = [i for i, part in enumerate(parts) if key(part) == key(leaf)]
        if len(matches) != 1:
            return None
        parts = parts[matches[0] + 1 :]
    return "/".join(parts) or None
