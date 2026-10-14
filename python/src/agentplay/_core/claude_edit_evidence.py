"""Recover full-line edit evidence from Claude's recorded successful patch result."""

from difflib import SequenceMatcher
from typing import Any

from agentplay._core.code_authorship import (
    MAX_EDITS_PER_CALL,
    MAX_HASHES_PER_CALL,
    extract_edit_evidence,
    line_fingerprints,
)


def result_patches(lines: list[dict]) -> dict[str, dict | None]:
    """Index native results; None fences invalid/conflicting IDs from legacy fallback."""
    results: dict[str, dict] = {}
    conflicting: set[str] = set()
    for line in lines:
        result = line.get("toolUseResult")
        message = line.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
        for block in blocks:
            key = block.get("tool_use_id")
            if not isinstance(key, str) or not key:
                continue
            if block.get("is_error"):
                conflicting.add(key)
            elif "toolUseResult" in line:
                if len(blocks) != 1 or not isinstance(result, dict):
                    conflicting.add(key)
                elif key in results and results[key] != result:
                    conflicting.add(key)
                else:
                    results[key] = result
    return {**results, **dict.fromkeys(conflicting)}


def call_edit_evidence(
    tool_name: Any, tool_input: Any, native: Any, *, has_native: bool, successful: bool
) -> dict:
    """Choose a Claude call's edit evidence: its native result when that is the truth.

    Edit always prefers its recorded patch: a present-but-unverifiable result
    yields ``invalid_edits`` rather than the weaker request-string capture. A
    Write whose result is an ``update`` (it overwrote an existing file) replays
    as that result's hunks, because a full-content ``write`` cannot be placed
    over a base; a fenced Write result (conflicting or invalid) yields
    ``invalid_edits`` like a fenced Edit. Every other call keeps request-based
    capture.

    Args:
        tool_name: The tool's name as recorded.
        tool_input: The tool_use input.
        native: This call's entry from ``result_patches`` (None when fenced).
        has_native: Whether ``result_patches`` has an entry for this call.
        successful: Whether the call produced a non-error result.

    Returns:
        A ``{"status", "edits"}`` evidence dict.
    """
    name = tool_name.lower() if isinstance(tool_name, str) else ""
    if (
        successful
        and has_native
        and (
            name == "edit"
            # None is result_patches' conflict fence: never fall back to the request.
            or (
                name == "write"
                and (native is None or isinstance(native, dict) and native.get("type") == "update")
            )
        )
    ):
        return extract_result_edit(tool_name, tool_input, native) or {
            "status": "invalid_edits",
            "edits": [],
        }
    return extract_edit_evidence(tool_name, tool_input, successful=successful)


def extract_result_edit(tool_name: str, tool_input: Any, result: Any) -> dict | None:
    """Hash a verified native Edit/overwriting-Write result; mismatches yield no evidence.

    The patch records the actual complete lines changed by the tool, including
    boundary text omitted from an Edit substring. A Write over an existing file
    records an ``update`` patch of the same shape, verified against the written
    content (and the original file when recorded). No current working-tree read
    is used to reconstruct historical content. No source text leaves this helper.
    """
    if (
        not isinstance(tool_name, str)
        or tool_name.lower() not in {"edit", "write"}
        or not isinstance(tool_input, dict)
        or not isinstance(result, dict)
    ):
        return None
    write = tool_name.lower() == "write"
    path = tool_input.get("file_path")
    if (
        not isinstance(path, str)
        or not path
        or result.get("filePath") != path
        or result.get("userModified") is not False
    ):
        return None
    if write:
        written = tool_input.get("content")
        if (
            result.get("type") != "update"
            or not isinstance(written, str)
            or result.get("content") != written
            or len(written) > 1_000_000
        ):
            return None
    elif (
        result.get("oldString") != tool_input.get("old_string")
        or result.get("newString") != tool_input.get("new_string")
        or not isinstance(tool_input.get("old_string"), str)
        or not tool_input["old_string"]
        or not isinstance(tool_input.get("new_string"), str)
        or type(result.get("replaceAll")) is not bool
        or type(tool_input.get("replace_all", False)) is not bool
        or result["replaceAll"] != tool_input.get("replace_all", False)
    ):
        return None
    hunks = result.get("structuredPatch")
    if not isinstance(hunks, list) or not 0 < len(hunks) <= MAX_EDITS_PER_CALL:
        return None
    original = result.get("originalFile")
    reference = None
    if write:
        if isinstance(original, str):
            if len(original) > 1_000_000:
                return None
            reference = (original.splitlines(), written.splitlines())
        elif original is not None:
            return None
    elif isinstance(original, str):
        if len(original) > 1_000_000 or tool_input["old_string"] not in original:
            return None
        replacements = original.count(tool_input["old_string"]) if result["replaceAll"] else 1
        if (
            len(original)
            + replacements * (len(tool_input["new_string"]) - len(tool_input["old_string"]))
            > 1_000_000
        ):
            return None
        updated = original.replace(
            tool_input["old_string"],
            tool_input["new_string"],
            -1 if result["replaceAll"] else 1,
        )
        reference = (original.splitlines(), updated.splitlines())
    elif original is not None:
        return None
    reconstructed = []
    next_original_line = 0
    direct_match = len(hunks) == 1 or result.get("replaceAll") is True
    edits = []
    total = 0
    total_chars = 0
    last_old = last_new = 0
    for hunk in hunks:
        if not isinstance(hunk, dict):
            return None
        if any(
            type(hunk.get(k)) is not int or hunk[k] < 0
            for k in ("oldStart", "newStart", "oldLines", "newLines")
        ):
            return None
        if hunk["oldStart"] == 0:
            return None
        if hunk["oldStart"] < last_old or hunk["newStart"] < last_new:
            return None
        last_old = hunk["oldStart"] + hunk["oldLines"]
        last_new = hunk["newStart"] + hunk["newLines"]
        lines = hunk.get("lines")
        if not isinstance(lines, list):
            return None
        before, after = [], []
        for line in lines:
            if (
                not isinstance(line, str)
                or not line
                or line[0] not in " +-"
                or len(line.splitlines()) != 1
                or line.splitlines()[0] != line
                or any(0xD800 <= ord(c) <= 0xDFFF for c in line)
            ):
                return None
            total_chars += len(line)
            if total_chars > 1_000_000:
                return None
            if line[0] != "+":
                before.append(line[1:])
            if line[0] != "-":
                after.append(line[1:])
        if (
            len(before) != hunk["oldLines"]
            or len(after) != hunk["newLines"]
            or not before
            or before == after
        ):
            return None
        if reference is not None:
            old_start = hunk["oldStart"] - 1
            new_start = max(0, hunk["newStart"] - 1)
            if (
                old_start < 0
                or reference[0][old_start : old_start + len(before)] != before
                or reference[1][new_start : new_start + len(after)] != after
            ):
                return None
            reconstructed.extend(reference[0][next_original_line:old_start])
            reconstructed.extend(after)
            next_original_line = old_start + len(before)
        if reference is None and write:
            # No original recorded: the full written content still pins every
            # hunk's after side to its exact position.
            new_start = max(0, hunk["newStart"] - 1)
            if written.splitlines()[new_start : new_start + len(after)] != after:
                return None
        elif reference is None:
            before_text = "\n".join(before) + "\n"
            after_text = "\n".join(after) + "\n" if after else ""
            direct_match = direct_match and _matches_overlapping_replacement(
                before_text,
                after_text,
                tool_input["old_string"],
                tool_input["new_string"],
                result["replaceAll"],
            )
        total += len(before) + len(after)
        if total > MAX_HASHES_PER_CALL:
            return None
        edits.append(
            {
                "path": path,
                "operation": "replace",
                "before": line_fingerprints("\n".join(before) + "\n"),
                "after": line_fingerprints("\n".join(after) + "\n") if after else [],
            }
        )
    if reference is not None:
        reconstructed.extend(reference[0][next_original_line:])
        if reconstructed != reference[1]:
            return None
    if (
        reference is None
        and not write
        and not direct_match
        and not _matches_requested_changes(
            hunks, tool_input["old_string"], tool_input["new_string"], result["replaceAll"]
        )
    ):
        return None
    return {"status": "captured", "edits": edits}


def _matches_overlapping_replacement(
    before: str, after: str, old: str, new: str, replace_all: bool
) -> bool:
    """Check a full replacement or a clipped patch with consistent shared context."""
    replacements = before.count(old) if replace_all else 1
    if len(before) + replacements * (len(new) - len(old)) > 1_000_000:
        return False
    if old in before and before.replace(old, new, -1 if replace_all else 1) == after:
        return True
    if replace_all or len(old) + len(new) > 1_000_000:
        return False
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    old_core = old[prefix : len(old) - suffix]
    new_core = new[prefix : len(new) - suffix]
    needle, haystack = (old_core, before) if old_core else (new_core, after)
    if not needle:
        return False
    position = haystack.find(needle)
    for _ in range(64):
        if position < 0:
            return False
        start = position - prefix
        if before[:position] + new_core + before[position + len(old_core) :] == after:
            if all(
                observed[max(0, start) : min(len(observed), start + len(requested))]
                == requested[max(0, -start) : min(len(requested), len(observed) - start)]
                for observed, requested in ((before, old), (after, new))
            ):
                return True
        position = haystack.find(needle, position + 1)
    return False


def _matches_requested_changes(hunks: list[dict], old: str, new: str, replace_all: bool) -> bool:
    """Validate clipped hunks against request changes, positions and overlapping context.

    Native patches can omit unchanged lines from a long replacement. Align each
    changed group with the request diff; only the request's boundary lines may
    have surrounding file text, and that surrounding text must remain unchanged.
    """
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if len(old) + len(new) > 1_000_000 or len(old_lines) + len(new_lines) > MAX_HASHES_PER_CALL:
        return False
    expected = [
        op
        for op in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    actual = []
    for hunk in hunks:
        old_pos, new_pos = hunk["oldStart"] - 1, max(0, hunk["newStart"] - 1)
        group = None
        for line in [*hunk["lines"], " "]:
            if line[0] == " ":
                if group is not None:
                    actual.append(group)
                    group = None
                old_pos += 1
                new_pos += 1
            else:
                if group is None:
                    group = [old_pos, new_pos, [], []]
                if line[0] == "-":
                    group[2].append(line[1:])
                    old_pos += 1
                else:
                    group[3].append(line[1:])
                    new_pos += 1
    if (
        not expected
        or not actual
        or len(actual) % len(expected)
        or (not replace_all and len(actual) != len(expected))
    ):
        return False
    for occurrence in range(len(actual) // len(expected)):
        offsets = None
        for group, (_, i1, i2, j1, j2) in zip(actual[occurrence * len(expected) :], expected, strict=False):
            before, after = "\n".join(group[2]), "\n".join(group[3])
            wanted_before, wanted_after = "\n".join(old_lines[i1:i2]), "\n".join(new_lines[j1:j2])
            position = before.find(wanted_before)
            matched = False
            while position >= 0:
                prefix, suffix = before[:position], before[position + len(wanted_before) :]
                if (
                    (not prefix or (i1 == j1 == 0 and "\n" not in prefix))
                    and (
                        not suffix
                        or (
                            i2 == len(old_lines)
                            and j2 == len(new_lines)
                            and not old.endswith("\n")
                            and not new.endswith("\n")
                            and "\n" not in suffix
                        )
                    )
                    and after == prefix + wanted_after + suffix
                    and (wanted_before or not before)
                ):
                    matched = True
                    break
                position = before.find(wanted_before, position + 1) if wanted_before else -1
            alignment = (group[0] - i1, group[1] - j1)
            if not matched or (offsets is not None and alignment != offsets):
                return False
            offsets = alignment
        if offsets[1] - offsets[0] != occurrence * (new.count("\n") - old.count("\n")):
            return False
        for hunk in hunks:
            for marker, start, offset, lines, text in (
                ("+", hunk["oldStart"] - 1, offsets[0], old_lines, old),
                ("-", max(0, hunk["newStart"] - 1), offsets[1], new_lines, new),
            ):
                visible = [line[1:] for line in hunk["lines"] if line[0] != marker]
                for index, line in enumerate(visible):
                    relative = start + index - offset
                    if not 0 <= relative < len(lines):
                        continue
                    expected_line = lines[relative]
                    if relative == 0 and relative == len(lines) - 1 and not text.endswith("\n"):
                        valid = expected_line in line
                    elif relative == 0:
                        valid = line.endswith(expected_line)
                    elif relative == len(lines) - 1 and not text.endswith("\n"):
                        valid = line.startswith(expected_line)
                    else:
                        valid = line == expected_line
                    if not valid:
                        return False
    return True
