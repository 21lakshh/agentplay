"""Successful native patches attribute substring edits without inventing line boundaries."""

import json
from copy import deepcopy

import pytest

from agentplay._core.claude_edit_evidence import (
    call_edit_evidence,
    extract_result_edit,
    result_patches,
)
from agentplay._core.code_authorship import attribute_file
from agentplay._core.ingestion.coding_agent._sources.local.parsers.claude_code import parse_claude_code


def fixture():
    args = {"file_path": "/repo/app.py", "old_string": "old", "new_string": "new"}
    result = {
        "filePath": "/repo/app.py",
        "oldString": "old",
        "newString": "new",
        "userModified": False,
        "replaceAll": False,
        "structuredPatch": [
            {
                "oldStart": 1,
                "oldLines": 2,
                "newStart": 1,
                "newLines": 2,
                "lines": ["-return old", "+return new", " context"],
            }
        ],
    }
    return args, result


def test_partial_line_native_result_survives_manual_git_and_other_session():
    args, result = fixture()
    evidence = extract_result_edit("Edit", args, result)
    assert evidence["status"] == "captured"
    assert "return new" not in json.dumps(evidence)
    edits = [{**evidence["edits"][0], "session_id": "agent-a", "event_id": "call-a"}]
    args2 = {**args, "old_string": "context", "new_string": "other"}
    result2 = {
        **result,
        "oldString": "context",
        "newString": "other",
        "structuredPatch": [
            {
                "oldStart": 1,
                "oldLines": 2,
                "newStart": 1,
                "newLines": 2,
                "lines": [" return new", "-context", "+other"],
            }
        ],
    }
    edits += [
        {
            **extract_result_edit("Edit", args2, result2)["edits"][0],
            "session_id": "agent-b",
            "event_id": "call-b",
        }
    ]
    assert attribute_file("return old\ncontext\n", "return new\nother\n", edits)["sessions"] == {
        "agent-a": 1,
        "agent-b": 1,
    }
    assert attribute_file("return old\ncontext\n", "manual\nother\n", edits)["sessions"] == {
        "agent-b": 1
    }
    assert (
        attribute_file("unrelated old\ncontext\n", "return new\nother\n", edits)["sessions"] == {}
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("filePath", "/foreign/app.py"),
        ("oldString", "wrong"),
        ("newString", "wrong"),
        ("userModified", True),
        ("userModified", None),
        ("structuredPatch", []),
    ],
)
def test_mismatch_never_yields_evidence(field, value):
    args, result = fixture()
    result[field] = value
    assert extract_result_edit("Edit", args, result) is None


@pytest.mark.parametrize(
    "mutation", ["counts", "prefix", "multiline", "overlap", "unicode", "surrogate"]
)
def test_invalid_native_patch_rejected(mutation):
    args, result = fixture()
    h = result["structuredPatch"][0]
    if mutation == "counts":
        h["oldLines"] = 99
    if mutation == "prefix":
        h["lines"][0] = "?old"
    if mutation == "multiline":
        h["lines"][0] = "-old\nspoof"
    if mutation == "overlap":
        result["structuredPatch"].append(deepcopy(h))
    if mutation == "unicode":
        h["lines"][0] = "-old\u2028spoof"
    if mutation == "surrogate":
        h["lines"][0] = "-old\ud800"
    assert extract_result_edit("Edit", args, result) is None


def transcript(failed=False, pending=False):
    args, result = fixture()
    rows = [
        {"type": "user", "message": {"role": "user", "content": "fix"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "call-a", "name": "Edit", "input": args}],
            },
        },
    ]
    if not pending:
        rows.append(
            {
                "type": "user",
                "toolUseResult": result,
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call-a",
                            "content": "done",
                            "is_error": failed,
                        }
                    ],
                },
            }
        )
    return [{**r, "timestamp": "2026-09-14T10:00:00Z"} for r in rows]


@pytest.mark.parametrize("failed,pending", [(False, False), (True, False), (False, True)])
def test_real_parser_uses_only_successful_result(failed, pending):
    rows = transcript(failed, pending)
    _, spans = parse_claude_code("\n".join(map(json.dumps, rows)).encode(), "agent-a")
    edit = next(s for s in spans if s.tool_name == "Edit").attributes["code.edit_evidence"]
    assert edit["status"] == ("not_successful" if failed or pending else "captured")


def test_conflicting_replay_does_not_select_arbitrary_patch():
    rows = transcript()
    other = deepcopy(rows[-1])
    other["toolUseResult"]["filePath"] = "/foreign.py"
    assert result_patches([*rows, other]) == {"call-a": None}
    assert result_patches([*rows, deepcopy(rows[-1])]) == {"call-a": rows[-1]["toolUseResult"]}


@pytest.mark.parametrize("first", [True, False])
@pytest.mark.parametrize("mutation", ["failed", "null", "conflicting", "ambiguous"])
def test_invalid_replay_blocks_native_and_legacy_capture(first, mutation):
    rows = transcript()
    args = rows[1]["message"]["content"][0]["input"]
    args.update(old_string="\nold\n", new_string="\nnew\n")
    rows[-1]["toolUseResult"].update(oldString="\nold\n", newString="\nnew\n")
    other = deepcopy(rows[-1])
    if mutation == "failed":
        other["message"]["content"][0]["is_error"] = True
        other.pop("toolUseResult")
    elif mutation == "null":
        other["toolUseResult"] = None
    elif mutation == "conflicting":
        other["toolUseResult"]["filePath"] = "/foreign.py"
    else:
        other["message"]["content"].append({"type": "tool_result", "tool_use_id": "other"})
    rows.insert(2 if first else len(rows), other)
    assert result_patches(rows)["call-a"] is None
    _, spans = parse_claude_code("\n".join(map(json.dumps, rows)).encode(), "agent-a")
    evidence = next(s for s in spans if s.tool_name == "Edit").attributes["code.edit_evidence"]
    assert evidence["edits"] == []
    assert evidence["status"] in {"invalid_edits", "not_successful"}


@pytest.mark.parametrize("mutation", ["modified", "invalid_patch", "absent"])
def test_legacy_fallback_only_when_native_result_absent(mutation):
    rows = transcript()
    args = rows[1]["message"]["content"][0]["input"]
    args.update(old_string="\nold\n", new_string="\nnew\n")
    rows[-1]["toolUseResult"].update(oldString="\nold\n", newString="\nnew\n")
    if mutation == "modified":
        rows[-1]["toolUseResult"]["userModified"] = True
    elif mutation == "invalid_patch":
        rows[-1]["toolUseResult"]["structuredPatch"] = []
    else:
        rows[-1].pop("toolUseResult")
    _, spans = parse_claude_code("\n".join(map(json.dumps, rows)).encode(), "agent-a")
    evidence = next(s for s in spans if s.tool_name == "Edit").attributes["code.edit_evidence"]
    assert evidence["status"] == ("captured" if mutation == "absent" else "invalid_edits")


def test_native_write_keeps_legacy_full_file_capture():
    rows = transcript()
    rows[1]["message"]["content"][0].update(
        name="Write", input={"file_path": "/repo/app.py", "content": "new\n"}
    )
    rows[-1]["toolUseResult"] = {"type": "create", "filePath": "/repo/app.py"}
    _, spans = parse_claude_code("\n".join(map(json.dumps, rows)).encode(), "agent-a")
    evidence = next(s for s in spans if s.tool_name == "Write").attributes["code.edit_evidence"]
    assert evidence["status"] == "captured"


@pytest.mark.parametrize("value", [None, 0, 1, "", "false", [], {}])
@pytest.mark.parametrize("field", ["replaceAll", "replace_all"])
def test_nonboolean_replacement_mode_is_not_verified(field, value):
    args, result = fixture()
    (result if field == "replaceAll" else args)[field] = value
    assert extract_result_edit("Edit", args, result) is None


def test_native_replacement_mode_required_but_optional_invocation_defaults_false():
    args, result = fixture()
    assert extract_result_edit("Edit", args, result)["status"] == "captured"
    args["replace_all"] = True
    assert extract_result_edit("Edit", args, result) is None
    result["replaceAll"] = True
    assert extract_result_edit("Edit", args, result)["status"] == "captured"
    del result["replaceAll"]
    assert extract_result_edit("Edit", args, result) is None


@pytest.mark.parametrize(
    "lines", [["-invented", "+credited", " context"], ["-return old", "+unrequested", " context"]]
)
def test_matching_metadata_cannot_validate_fabricated_patch_contents(lines):
    args, result = fixture()
    result["structuredPatch"][0]["lines"] = lines
    assert extract_result_edit("Edit", args, result) is None


def test_original_file_validates_split_patch_for_one_long_replacement():
    args, result = fixture()
    before = "old\n" + "unchanged\n" * 10 + "last\n"
    after = "new\n" + "unchanged\n" * 10 + "changed\n"
    args.update(old_string=before, new_string=after)
    result.update(
        oldString=before,
        newString=after,
        originalFile=before,
        structuredPatch=[
            {"oldStart": 1, "newStart": 1, "oldLines": 1, "newLines": 1, "lines": ["-old", "+new"]},
            {
                "oldStart": 12,
                "newStart": 12,
                "oldLines": 1,
                "newLines": 1,
                "lines": ["-last", "+changed"],
            },
        ],
    )
    assert extract_result_edit("Edit", args, result)["status"] == "captured"
    incomplete = deepcopy(result)
    incomplete["structuredPatch"].pop()
    assert extract_result_edit("Edit", args, incomplete) is None
    without_original = deepcopy(result)
    del without_original["originalFile"]
    assert extract_result_edit("Edit", args, without_original)["status"] == "captured"
    result["structuredPatch"][1]["lines"][-1] = "+fabricated"
    assert extract_result_edit("Edit", args, result) is None
    result["structuredPatch"][1]["lines"][-1] = "+changed"
    result["structuredPatch"][1]["newStart"] = 11
    assert extract_result_edit("Edit", args, result) is None
    del result["originalFile"]
    assert extract_result_edit("Edit", args, result) is None


def test_clipped_patch_checks_replacement_and_all_overlapping_context():
    args, result = fixture()
    args.update(
        old_string="omitted\nreturn old\ncontext\nomitted tail",
        new_string="omitted\nreturn new\ncontext\nomitted tail",
    )
    result.update(oldString=args["old_string"], newString=args["new_string"])
    assert extract_result_edit("Edit", args, result)["status"] == "captured"
    result["structuredPatch"][0]["lines"][-1] = " fabricated context"
    assert extract_result_edit("Edit", args, result) is None


def test_clipped_replace_all_preserves_offsets_across_occurrences():
    args, result = fixture()
    args.update(old_string="start\nold\nend", new_string="start\nnew\nend", replace_all=True)
    result.update(
        oldString=args["old_string"],
        newString=args["new_string"],
        replaceAll=True,
        structuredPatch=[
            {"oldStart": 2, "newStart": 2, "oldLines": 1, "newLines": 1, "lines": ["-old", "+new"]},
            {
                "oldStart": 12,
                "newStart": 12,
                "oldLines": 1,
                "newLines": 1,
                "lines": ["-old", "+new"],
            },
        ],
    )
    assert extract_result_edit("Edit", args, result)["status"] == "captured"
    result["structuredPatch"][1]["newStart"] = 13
    assert extract_result_edit("Edit", args, result) is None


@pytest.mark.parametrize("no_op", [False, True])
def test_original_snapshot_does_not_make_context_only_hunks_edit_evidence(no_op):
    args, result = fixture()
    result["originalFile"] = "return old\ncontext\n"
    if no_op:
        args["new_string"] = args["old_string"]
        result["newString"] = result["oldString"]
    result["structuredPatch"] = [
        {"oldStart": 2, "newStart": 2, "oldLines": 1, "newLines": 1, "lines": [" context"]}
    ]
    assert extract_result_edit("Edit", args, result) is None


def test_zero_old_start_without_original_file_is_rejected():
    args, result = fixture()
    result["structuredPatch"][0]["oldStart"] = 0
    assert extract_result_edit("Edit", args, result) is None


def write_update(original="a\nold\nz\n", written="a\nnew\nz\n"):
    """A Write that overwrote an existing file, shaped like Claude Code's record."""
    args = {"file_path": "/repo/app.py", "content": written}
    result = {
        "type": "update",
        "filePath": "/repo/app.py",
        "content": written,
        "userModified": False,
        "originalFile": original,
        "structuredPatch": [
            {
                "oldStart": 1,
                "oldLines": 3,
                "newStart": 1,
                "newLines": 3,
                "lines": [" a", "-old", "+new", " z"],
            }
        ],
    }
    return args, result


@pytest.mark.parametrize("original", ["a\nold\nz\n", None])
def test_write_over_existing_file_replays_as_its_patch(original):
    args, result = write_update(original)
    evidence = call_edit_evidence("Write", args, result, has_native=True, successful=True)
    assert evidence["status"] == "captured"
    assert [e["operation"] for e in evidence["edits"]] == ["replace"]
    edits = [{**evidence["edits"][0], "session_id": "agent-a", "event_id": "call-a"}]
    attributed = attribute_file("a\nold\nz\n", "a\nnew\nz\n", edits)
    assert attributed["sessions"] == {"agent-a": 1}
    assert attributed["unresolved_edits"] == []
    # The full-content capture it replaces cannot be placed over a base.
    legacy = call_edit_evidence("Write", args, None, has_native=False, successful=True)
    legacy_edits = [{**legacy["edits"][0], "session_id": "agent-a", "event_id": "call-a"}]
    assert attribute_file("a\nold\nz\n", "a\nnew\nz\n", legacy_edits)["unresolved_edits"] == [
        {"event_id": "call-a", "reason": "missing_before_snapshot"}
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a, r: r.update(content="a\nnew\nz\nextra\n"),  # recorded != requested
        lambda a, r: r.update(filePath="/repo/other.py"),
        lambda a, r: r.update(userModified=True),
        lambda a, r: r.update(originalFile="a\ndifferent\nz\n"),  # patch misplaced
        lambda a, r: r["structuredPatch"][0].update(newStart=2),  # after side misplaced
        lambda a, r: r.update(originalFile=None, content="x\nnew\nz\n")
        or a.update(content="x\nnew\nz\n"),
    ],
)
def test_unverifiable_write_update_yields_no_evidence(mutate):
    args, result = write_update()
    mutate(args, result)
    evidence = call_edit_evidence("Write", args, result, has_native=True, successful=True)
    assert evidence == {"status": "invalid_edits", "edits": []}


def test_write_create_and_absent_result_keep_full_file_capture():
    args, result = write_update()
    for native, has_native in (({**result, "type": "create"}, True), (None, False)):
        evidence = call_edit_evidence("Write", args, native, has_native=has_native, successful=True)
        assert [e["operation"] for e in evidence["edits"]] == ["write"]


def test_fenced_write_result_never_falls_back_to_the_request():
    args, _ = write_update()
    evidence = call_edit_evidence("Write", args, None, has_native=True, successful=True)
    assert evidence == {"status": "invalid_edits", "edits": []}


def test_real_parser_turns_overwriting_write_into_replace():
    rows = transcript()
    args, result = write_update()
    rows[1]["message"]["content"][0].update(name="Write", input=args)
    rows[-1]["toolUseResult"] = result
    _, spans = parse_claude_code("\n".join(map(json.dumps, rows)).encode(), "agent-a")
    evidence = next(s for s in spans if s.tool_name == "Write").attributes["code.edit_evidence"]
    assert evidence["status"] == "captured"
    assert [e["operation"] for e in evidence["edits"]] == ["replace"]
