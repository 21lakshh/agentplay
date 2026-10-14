"""Observed edits survive external commits without confusing creation or usage."""

import json

from agentplay._core.code_authorship import (
    attribute_file,
    extract_edit_evidence,
    inserted_hashes,
    line_fingerprints,
)


def edit(old, new, *, session="agent-a", event="edit-1"):
    captured = extract_edit_evidence(
        "Edit",
        {"file_path": "app.py", "old_string": "\n" + old + "\n", "new_string": "\n" + new + "\n"},
        successful=True,
    )
    return {**captured["edits"][0], "session_id": session, "event_id": event}


def test_manual_commit_and_pr_need_no_agent_shell_commands():
    result = attribute_file(
        "return old\n", "return improved\n", [edit("return old", "return improved")]
    )
    assert result["agent_added_lines"] == 1
    assert result["unknown_added_lines"] == 0
    assert result["sessions"] == {"agent-a": 1}


def test_pr_creation_is_not_code_evidence():
    evidence = extract_edit_evidence("Bash", '{"command":"gh pr create"}', successful=True)
    assert evidence["edits"] == []
    result = attribute_file("", "human implementation\n", [])
    assert result["agent_added_lines"] == 0
    assert result["unknown_added_lines"] == 1


def test_human_replacement_removes_prior_agent_ownership():
    result = attribute_file("old\n", "human replacement\n", [edit("old", "agent replacement")])
    assert result["agent_added_lines"] == 0
    assert result["unknown_added_lines"] == 1


def test_human_addition_preserves_surviving_agent_lines():
    result = attribute_file(
        "old\n", "agent replacement\nhuman addition\n", [edit("old", "agent replacement")]
    )
    assert result["sessions"] == {"agent-a": 1}
    assert result["added_lines"] == 2
    assert result["unknown_added_lines"] == 1


def test_sequential_agents_do_not_double_count_the_same_line():
    result = attribute_file(
        "old\n",
        "final\n",
        [edit("old", "interim"), edit("interim", "final", session="agent-b", event="edit-2")],
    )
    assert result["sessions"] == {"agent-b": 1}
    assert result["event_ids"] == ["edit-2"]


def test_multiple_agents_keep_their_distinct_contributions():
    result = attribute_file(
        "one\ntwo\n",
        "first\nsecond\n",
        [edit("one", "first"), edit("two", "second", session="agent-b", event="edit-2")],
    )
    assert result["sessions"] == {"agent-a": 1, "agent-b": 1}
    assert result["added_lines"] == 2


def test_ambiguous_before_does_not_choose_a_duplicate():
    result = attribute_file("same\nsame\n", "replacement\nsame\n", [edit("same", "replacement")])
    assert result["agent_added_lines"] == 0
    assert result["unresolved_edits"][0]["reason"] == "before_not_unique_or_missing"


def test_failed_edit_never_emits_evidence():
    result = extract_edit_evidence(
        "Edit", {"file_path": "app.py", "old_string": "old", "new_string": "new"}, successful=False
    )
    assert result == {"status": "not_successful", "edits": []}


def test_patch_fingerprints_never_contain_source_content():
    captured = extract_edit_evidence(
        "apply_patch",
        "*** Begin Patch\n*** Update File: app.py\n@@\n-old unique secret text\n+new unique secret text\n*** End Patch",
        successful=True,
    )
    assert captured["status"] == "captured"
    assert "unique secret text" not in json.dumps(captured)
    evidence = {**captured["edits"][0], "session_id": "agent-a", "event_id": "patch-1"}
    assert (
        attribute_file("old unique secret text\n", "new unique secret text\n", [evidence])[
            "agent_added_lines"
        ]
        == 1
    )


def test_new_file_write_matches_without_commit_identity():
    captured = extract_edit_evidence(
        "Write", {"file_path": "app.py", "content": "first\nsecond\n"}, successful=True
    )
    evidence = {**captured["edits"][0], "session_id": "agent-a", "event_id": "write-1"}
    assert attribute_file(None, "first\nsecond\n", [evidence])["agent_added_lines"] == 2


def test_existing_file_write_without_before_stays_unknown():
    captured = extract_edit_evidence(
        "Write", {"file_path": "app.py", "content": "human and agent changes\n"}, successful=True
    )
    evidence = {**captured["edits"][0], "session_id": "agent-a", "event_id": "write-1"}
    assert (
        attribute_file("old\n", "human and agent changes\n", [evidence])["agent_added_lines"] == 0
    )


def test_capture_limit_does_not_emit_a_partial_success():
    evidence = extract_edit_evidence(
        "Write", {"file_path": "app.py", "content": "line\n" * 2049}, successful=True
    )
    assert evidence == {"status": "capture_limit", "edits": []}


def test_identical_edits_from_distinct_sessions_remain_ambiguous():
    result = attribute_file(
        "old\n", "new\n", [edit("old", "new"), edit("old", "new", session="agent-b", event="other")]
    )
    assert result["sessions"] == {}
    assert result["deletion_sessions"] == {}
    assert result["unknown_added_lines"] == 1


def test_deletion_only_has_independent_session_evidence():
    result = attribute_file("keep\nremove\n", "keep\n", [edit("keep\nremove", "keep")])
    assert result["added_lines"] == 0
    assert result["agent_deleted_lines"] == 1
    assert result["deletion_sessions"] == {"agent-a": 1}
    assert result["deletion_event_ids"] == ["edit-1"]
    assert result["event_ids"] == []


def test_reintroduced_deleted_text_is_not_agent_deletion():
    result = attribute_file("keep\nremove\n", "remove\nkeep\n", [edit("keep\nremove", "keep")])
    assert result["agent_deleted_lines"] == 0


def test_filename_only_delete_is_not_content_evidence():
    captured = extract_edit_evidence(
        "apply_patch", "*** Begin Patch\n*** Delete File: app.py\n*** End Patch", successful=True
    )
    result = attribute_file(
        "old\n", None, [{**captured["edits"][0], "session_id": "agent-a", "event_id": "delete"}]
    )
    assert result["agent_deleted_lines"] == 0
    assert result["unknown_deleted_lines"] == 1


def test_replacement_preserves_trailing_blank_lines():
    from hashlib import sha256

    evidence = extract_edit_evidence(
        "Edit",
        {"path": "app.py", "old_string": "\nold\n\n", "new_string": "\nnew\n\n"},
        successful=True,
    )
    empty = sha256(b"").hexdigest()
    assert evidence["edits"][0]["before"][-1] == empty
    assert evidence["edits"][0]["after"][-1] == empty
    assert len(evidence["edits"][0]["after"]) == 2
    result = attribute_file(
        "old\n\n",
        "new\n\n",
        [{**evidence["edits"][0], "session_id": "agent-a", "event_id": "blank-lines"}],
    )
    assert result["agent_added_lines"] == 1


def test_replacing_a_blank_line_preserves_its_fingerprint():
    from hashlib import sha256

    evidence = extract_edit_evidence(
        "Edit", {"path": "app.py", "old_string": "\n\n", "new_string": "\nnew\n"}, successful=True
    )
    assert evidence["edits"][0]["before"] == [sha256(b"").hexdigest()]


def test_kiro_requires_boolean_success_and_supports_fswrite_alias():
    from datetime import UTC, datetime

    from agentplay._core.ingestion.coding_agent._sources.local.parsers.kiro import _build_tool_span

    now = datetime.now(UTC)
    call = {
        "toolName": "fsWrite",
        "args": {"path": "app.py", "text": "new\n"},
        "toolCallId": "call-a",
    }
    for result in (
        None,
        {},
        {"success": None},
        {"success": False},
        {"success": "true"},
        {"success": 1},
    ):
        span = _build_tool_span("session-a", "turn-a", call, result, now, now)
        assert span.attributes["code.edit_evidence"] == {"status": "not_successful", "edits": []}
    span = _build_tool_span("session-a", "turn-a", call, {"success": True}, now, now)
    assert span.name == "Write"
    assert span.attributes["code.edit_evidence"]["status"] == "captured"


def test_notebook_and_streaming_snippets_do_not_fabricate_before_snapshots():
    for name, args in (
        ("NotebookEdit", {"notebook_path": "example.ipynb", "new_source": "new"}),
        ("Edit", {"path": "app.py", "streamingContent": "// ... existing code ...\nnew"}),
    ):
        assert extract_edit_evidence(name, args, successful=True)["edits"] == []




def test_replacement_line_endings_match_file_fingerprints_without_losing_blanks():
    from agentplay._core.code_authorship import line_fingerprints

    for ending in (
        "\n",
        "\r\n",
        "\r",
        "\v",
        "\f",
        "\x1c",
        "\x1d",
        "\x1e",
        "\x85",
        "\u2028",
        "\u2029",
    ):
        old = ending + "old" + ending + ending
        new = ending + "new" + ending + ending
        captured = extract_edit_evidence(
            "Edit", {"path": "app.py", "old_string": old, "new_string": new}, successful=True
        )
        assert captured["status"] == "captured", repr(ending)
        evidence = captured["edits"][0]
        assert evidence["before"] == line_fingerprints("old" + ending + ending)
        assert evidence["after"] == line_fingerprints("new" + ending + ending)
        result = attribute_file(
            "old" + ending + ending,
            "new" + ending + ending,
            [{**evidence, "session_id": "agent-a", "event_id": "endings"}],
        )
        assert result["agent_added_lines"] == 1, repr(ending)


def test_native_rename_retains_verified_edit_at_destination():
    captured = extract_edit_evidence(
        "apply_patch",
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n@@\n-old unique implementation\n+new unique implementation\n*** End Patch",
        successful=True,
    )
    assert captured["status"] == "captured"
    assert captured["edits"][0]["path"] == "new.py"
    evidence = {**captured["edits"][0], "session_id": "agent-a", "event_id": "move-edit"}
    result = attribute_file(
        "old unique implementation\n", "new unique implementation\n", [evidence]
    )
    assert result["sessions"] == {"agent-a": 1}


def test_rename_without_content_does_not_claim_authorship():
    captured = extract_edit_evidence(
        "apply_patch",
        "*** Begin Patch\n*** Update File: old.py\n*** Move to: new.py\n*** End Patch",
        successful=True,
    )
    assert captured == {"status": "no_edit", "edits": []}


def test_uncaptured_formatting_does_not_claim_semantic_equivalence():
    result = attribute_file(
        "return old\n",
        "return  improved\n",
        [edit("return old", "return improved")],
    )
    assert result["sessions"] == {}
    assert result["unknown_added_lines"] == 1


def test_captured_formatting_preserves_traceable_session_contributions():
    result = attribute_file(
        "return old\n",
        "return  improved\n",
        [
            edit("return old", "return improved"),
            edit("return improved", "return  improved", session="formatter", event="format"),
        ],
    )
    assert result["sessions"] == {"formatter": 1}


def _replace(before, after, session, event, completed_at=None):
    return {
        "path": "app.py",
        "operation": "replace",
        "before": line_fingerprints(before),
        "after": line_fingerprints(after),
        "session_id": session,
        "event_id": event,
        "completed_at": completed_at,
    }


def test_inserted_hashes_ignore_context_and_whole_file_writes_over_content():
    replace = _replace("keep\nold\n", "keep\nnew\n", "a", "e1")
    write = {**_replace("", "keep\nwritten\n", "b", "e2"), "operation": "write"}
    assert inserted_hashes([replace, write], added_file=False) == {
        "a": set(line_fingerprints("new\n")),
        "b": set(),
    }
    assert inserted_hashes([write], added_file=True) == {
        "b": set(line_fingerprints("keep\nwritten\n"))
    }


def test_same_insertion_by_two_sessions_is_shared_and_unique_lines_stay_exclusive():
    # Best-of-N: both sessions insert "shared"; only session a also wrote "only_a".
    edits = [
        _replace("x\n", "x\nshared\nonly_a\n", "a", "a1", "2026-09-20T10:00:00+00:00"),
        _replace("y\n", "y\nshared\n", "b", "b1", "2026-09-20T11:00:00+00:00"),
    ]
    result = attribute_file("x\ny\n", "x\nshared\nonly_a\ny\nshared\n", edits)
    assert result["sessions"] == {"a": 2, "b": 1}
    assert result["exclusive_lines"] == {"a": 1}
    assert result["shared_lines"] == {"a": 1, "b": 1}
    assert result["session_events"] == {
        "a": {"a1": "2026-09-20T10:00:00+00:00"},
        "b": {"b1": "2026-09-20T11:00:00+00:00"},
    }


def test_exact_linked_owner_wins_a_shared_line_tie():
    edits = [
        _replace("x\n", "x\nshared\nonly_a\n", "a", "a1"),
        _replace("y\n", "y\nshared\n", "b", "b1"),
    ]
    result = attribute_file(
        "x\ny\n", "x\nshared\nonly_a\ny\nshared\n", edits, exact_sessions=frozenset({"a"})
    )
    assert result["exclusive_lines"] == {"a": 2}
    assert result["shared_lines"] == {"b": 1}


def test_deletions_never_count_toward_link_strength():
    # Session a deletes five base lines and adds one; b only deletes.
    base = "keep\n" + "".join(f"gone{i}\n" for i in range(5)) + "drop\n"
    edits = [
        _replace("keep\n" + "".join(f"gone{i}\n" for i in range(5)), "keep\nnew\n", "a", "a1"),
        _replace("keep\nnew\ndrop\n", "keep\nnew\n", "b", "b1"),
    ]
    result = attribute_file(base, "keep\nnew\n", edits)
    assert result["deletion_sessions"] == {"a": 5, "b": 1}
    assert result["sessions"] == {"a": 1}
    assert result["exclusive_lines"] == {"a": 1}
    assert result["shared_lines"] == {}
    # Deletion events stay evidence for the session even without line credit.
    assert result["session_events"] == {"a": {"a1": None}, "b": {"b1": None}}


def test_line_replay_handles_a_twenty_two_thousand_line_file():
    changed = range(0, 22_000, 1_000)
    base = "".join(f"line {n}\n" for n in range(22_000))
    head = "".join(f"agent {n}\n" if n in changed else f"line {n}\n" for n in range(22_000))
    chain = [_replace(f"line {n}\n", f"agent {n}\n", "a", f"e{n}") for n in changed]
    result = attribute_file(base, head, chain)
    assert result["sessions"] == {"a": 22}
    assert result["unresolved_edits"] == []
