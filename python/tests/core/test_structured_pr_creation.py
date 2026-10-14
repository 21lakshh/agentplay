"""Structured creation requests must match successful native tool results."""

import json

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers.claude_code import parse_claude_code
from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex
from agentplay._core.ingestion.coding_agent.produced import extract_created_prs


def parse(
    agent,
    output,
    *,
    name="mcp__github__create_pull_request",
    failed=False,
    completed=True,
    tool_args=None,
    return_spans=False,
):
    args = {"owner": "Owner", "repo": "Repo", "head": "feature", "base": "main", "body": "x" * 3000}
    if tool_args is not None:
        args = tool_args
    if agent == "claude":
        rows = [
            {"type": "user", "message": {"role": "user", "content": "open PR"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "create", "name": name, "input": args},
                    ],
                },
            },
        ]
        if completed:
            rows.append(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "create",
                                "content": output,
                                "is_error": failed,
                            },
                        ],
                    },
                }
            )
        parser = parse_claude_code
    elif agent == "cursor":
        from agentplay._core.ingestion.coding_agent._sources.local.parsers.cursor_store import (
            parse_cursor_store,
        )

        rows = [
            {"type": "cursor_store_session", "cwd": "/repo", "createdAt": 1790006400000},
            {"role": "user", "content": "open PR"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool-call", "toolCallId": "create", "toolName": name, "args": args}
                ],
            },
        ]
        if completed:
            rows.append(
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "create",
                            "result": output,
                            "isError": failed,
                        }
                    ],
                }
            )
        parser = parse_cursor_store
    elif agent == "copilot":
        from agentplay._core.ingestion.coding_agent._sources.local.parsers.copilot import parse_copilot

        rows = [
            {"type": "user.message", "data": {"content": "open PR"}},
            {
                "type": "tool.execution_start",
                "data": {"toolCallId": "create", "toolName": name, "arguments": args},
            },
        ]
        if completed:
            rows.append(
                {
                    "type": "tool.execution_complete",
                    "data": {
                        "toolCallId": "create",
                        "success": not failed,
                        "result": {"content": output},
                    },
                }
            )
        parser = parse_copilot
    elif agent == "kiro":
        from agentplay._core.ingestion.coding_agent._sources.local.parsers.kiro import parse_kiro

        rows = [
            {"type": "kiro_session", "id": "session"},
            {"payload": {"type": "user", "content": "edit"}},
            {
                "payload": {
                    "type": "tool_call",
                    "toolCallId": "edit",
                    "toolName": name,
                    "args": args,
                }
            },
        ]
        if completed:
            rows.append(
                {
                    "payload": {
                        "type": "tool_result",
                        "toolCallId": "edit",
                        "success": not failed,
                        "content": output,
                    }
                }
            )
        parser = parse_kiro
    elif agent == "opencode":
        from agentplay._core.ingestion.coding_agent._sources.local.parsers.opencode import (
            parse_opencode,
        )

        rows = [
            {
                "id": "u",
                "message": {"role": "user", "time": {"created": 1000}},
                "parts": [{"type": "text", "text": "open PR"}],
            },
            {
                "id": "a",
                "message": {"role": "assistant", "time": {"created": 2000}},
                "parts": [
                    {
                        "type": "tool",
                        "tool": name,
                        "callID": "create",
                        "state": {
                            "input": args,
                            "output": output,
                            "status": "error"
                            if failed
                            else "completed"
                            if completed
                            else "running",
                        },
                    }
                ],
            },
        ]
        parser = parse_opencode
    else:
        rows = [
            {"type": "session_meta", "payload": {"id": "session", "cwd": "/repo"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "open PR"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "create",
                    "name": name,
                    "arguments": json.dumps(args),
                },
            },
        ]
        if agent == "codex-wrapper":
            rows[-1]["payload"].update(
                name="exec", arguments="text(await tools." + name + "(" + json.dumps(args) + "));"
            )
            result = {"content": output} if isinstance(output, list) else json.loads(output)
            output = [{"type": "text", "text": json.dumps(result)}]
        if completed:
            rows.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "create",
                        "output": output,
                    },
                }
            )
        parser = parse_codex
    raw = b"\n".join(json.dumps({**r, "timestamp": "2026-09-21T00:00:00Z"}).encode() for r in rows)
    _, spans = parser(raw, "session")
    return spans if return_spans else extract_created_prs(spans)


@pytest.mark.parametrize(
    "agent", ["claude", "codex", "codex-wrapper", "cursor", "copilot", "opencode"]
)
@pytest.mark.parametrize("wrapped", [False, True])
def test_successful_structured_creation_survives_display_truncation(agent, wrapped):
    result = json.dumps(
        {"body": "x" * 4000, "html_url": "https://github.com/owner/repo/pull/9", "number": 9}
    )
    if wrapped:
        result = [{"type": "text", "text": result}]
    [created] = parse(agent, result)
    assert created.number == 9
    assert created.repo_name == "owner/repo"
    assert created.source_event_id


@pytest.mark.parametrize(
    "agent", ["claude", "codex", "codex-wrapper", "cursor", "copilot", "opencode"]
)
@pytest.mark.parametrize(
    "result",
    [
        {"html_url": "https://github.com/other/repo/pull/9", "number": 9},
        {"html_url": "https://github.com/owner/repo/pull/9", "number": 8},
        {"html_url": "https://github.com/owner/repo/pull/9", "error": "already exists"},
        {"html_url": "https://github.com/owner/repo/pull/9", "isError": True},
        {"html_url": "https://github.com/owner/repo/pull/9", "number": True},
    ],
)
def test_conflicting_or_failed_structured_result_never_links(agent, result):
    assert parse(agent, json.dumps(result)) == []


@pytest.mark.parametrize(
    "agent", ["claude", "codex", "codex-wrapper", "cursor", "copilot", "opencode"]
)
def test_reads_and_unfinished_creates_never_link(agent):
    result = json.dumps({"html_url": "https://github.com/owner/repo/pull/9", "number": 9})
    assert parse(agent, result, name="mcp__github__pull_request_read") == []
    assert parse(agent, result, completed=False) == []


@pytest.mark.parametrize("agent", ["claude", "cursor", "copilot", "opencode"])
def test_failed_tool_result_never_links(agent):
    assert (
        parse(agent, json.dumps({"html_url": "https://github.com/owner/repo/pull/9"}), failed=True)
        == []
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "gh api repos/owner/repo/pulls -X POST -f title=change -f head=feature -f base=main",
            True,
        ),
        ("gh api /repos/owner/repo/pulls --method POST --input body.json", True),
        ("gh api repos/owner/repo/pulls -X GET", False),
        ("gh api repos/owner/repo/pulls -X POST; echo fake", False),
        ("gh api repos/owner/repo/pulls -X POST -f title=x;echo fake", False),
        ("gh api repos/owner/repo/pulls -X POST\necho fake", False),
        ("gh api repos/owner/repo/pulls -X POST --hostname github.example", False),
    ],
)
def test_explicit_rest_creation_requires_single_successful_request(command, expected):
    from datetime import UTC, datetime

    from agentplay._core.ingestion.coding_agent.produced import (
        creation_command_attributes,
        creation_output_attributes,
    )
    from agentplay._core.ingestion.coding_agent.spans import SpanData

    request = creation_command_attributes({"command": command}, "Bash")
    output = json.dumps({"html_url": "https://github.com/owner/repo/pull/9", "number": 9})
    response = creation_output_attributes(
        output, request.get("pr.create.provider"), request.get("pr.create.repository")
    )
    now = datetime.now(UTC)
    span = SpanData(
        "event",
        "session",
        None,
        "Bash",
        "TOOL",
        "OK",
        now,
        now,
        {
            "tool.name": "Bash",
            "input.value": json.dumps({"command": command}),
            "output.value": output,
            **request,
            **response,
        },
    )
    assert bool(extract_created_prs([span])) is expected
    if expected:
        span.attributes["tool.name"] = "Read"
        assert extract_created_prs([span]) == []
        span.attributes["tool.name"] = "Bash"
        span.status_code = "ERROR"
        assert extract_created_prs([span]) == []


def test_codex_shell_rest_creation_uses_completed_process_body():
    args = {"cmd": "gh api repos/owner/repo/pulls -X POST -f head=feature -f base=main"}
    response = json.dumps(
        {"body": "x" * 4000, "html_url": "https://github.com/owner/repo/pull/9", "number": 9}
    )
    records = [
        {"type": "session_meta", "payload": {"id": "session", "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "create"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "create",
                "arguments": json.dumps(args),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "create",
                "output": json.dumps({"output": response, "exit_code": 0, "wall_time_seconds": 1}),
            },
        },
    ]
    raw = b"\n".join(
        json.dumps({**row, "timestamp": "2026-09-21T00:00:00Z"}).encode() for row in records
    )
    _, spans = parse_codex(raw, "session")
    tool = next(s for s in spans if s.tool_name == "Bash")
    assert tool.attributes["pr.create.output_url"] == "https://github.com/owner/repo/pull/9"
    assert "https://github.com/owner/repo/pull/9" not in tool.attributes["output.value"]
    assert [created.number for created in extract_created_prs(spans)] == [9]


@pytest.mark.parametrize(
    "name",
    [
        "mcp__evil__create_pull_request",
        "mcp__gitlab__create_pull_request",
        "create_pull_request",
        "githubish.create_pull_request",
    ],
)
def test_unknown_provider_never_becomes_github_creation(name):
    assert (
        parse(
            "claude",
            json.dumps({"html_url": "https://github.com/owner/repo/pull/9", "number": 9}),
            name=name,
        )
        == []
    )


@pytest.mark.parametrize("tool_name", ["Edit", "Write"])
@pytest.mark.parametrize("status", ["OK", "ERROR", "UNSET"])
def test_only_completed_mutations_count_as_useful_edits(tool_name, status):
    from datetime import UTC, datetime

    from agentplay._core.ingestion.coding_agent.metrics import compute_task_counters
    from agentplay._core.ingestion.coding_agent.spans import SpanData

    now = datetime.now(UTC)
    span = SpanData(
        "event",
        "session",
        "session:turn:1",
        tool_name,
        "TOOL",
        status,
        now,
        now,
        {"tool.name": tool_name, "input.value": json.dumps({"file_path": "src.py"})},
    )
    counters = compute_task_counters([span], start_turn=1, end_turn=1)
    assert counters.useful_edit_count == int(status == "OK")
    assert counters.error_count == int(status == "ERROR")


@pytest.mark.parametrize("failed", [False, True])
def test_copilot_preserves_mcp_error_envelope(failed):
    from agentplay._core.ingestion.coding_agent._sources.local.parsers.copilot import parse_copilot

    body = {
        "isError": failed,
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {"html_url": "https://github.com/owner/repo/pull/9", "number": 9}
                ),
            }
        ],
    }
    rows = [
        {
            "type": "tool.execution_start",
            "data": {
                "toolCallId": "create",
                "toolName": "mcp__github__create_pull_request",
                "arguments": {"owner": "owner", "repo": "repo"},
            },
        },
        {
            "type": "tool.execution_complete",
            "data": {"toolCallId": "create", "success": True, "result": body},
        },
    ]
    _, spans = parse_copilot(
        b"\n".join(json.dumps({**r, "timestamp": "2026-09-21T00:00:00Z"}).encode() for r in rows),
        "session",
    )
    assert bool(extract_created_prs(spans)) is not failed


@pytest.mark.parametrize(
    "agent", ["claude", "codex", "cursor", "copilot", "opencode", "kiro"]
)
@pytest.mark.parametrize("name", ["Edit", "Write"])
@pytest.mark.parametrize("completed", [True, False])
def test_native_mutation_completion_controls_useful_edit_metrics(agent, name, completed):
    from agentplay._core.ingestion.coding_agent.metrics import compute_task_counters

    spans = parse(
        agent,
        "done",
        name=name,
        completed=completed,
        tool_args={
            "file_path": "src.py",
            "old_string": "before",
            "new_string": "after",
            "content": "after",
        },
        return_spans=True,
    )
    [tool] = [s for s in spans if s.tool_name == name]
    assert tool.status_code == ("OK" if completed else "UNSET")
    counters = compute_task_counters(spans, start_turn=1, end_turn=1)
    assert counters.useful_edit_count == int(completed)


@pytest.mark.parametrize("agent", ["claude", "codex", "cursor", "copilot", "opencode"])
@pytest.mark.parametrize(
    "selector",
    ["-R owner/repo", "--repo=owner/repo", "-Rowner/repo", "--repo github.com/owner/repo"],
)
def test_explicit_shell_repository_must_match_created_url(agent, selector):
    command = "gh pr create --body '" + "x" * 2500 + "' " + selector
    args = {"command": command} if agent != "codex" else {"cmd": command}
    name = "exec_command" if agent == "codex" else "bash"
    good = "https://github.com/owner/repo/pull/9"
    bad = "https://github.com/other/repo/pull/9"
    assert len(parse(agent, good, name=name, tool_args=args)) == 1
    assert parse(agent, bad, name=name, tool_args=args) == []


@pytest.mark.parametrize(
    "command,accepted",
    [
        ("GH_REPO=owner/repo gh pr create", True),
        ("GH_REPO=other/repo gh pr create", False),
        ("GH_REPO=other/repo gh pr create -R owner/repo", True),
        ("GH_HOST=enterprise.example gh pr create -R owner/repo", False),
        ("gh pr create -R owner/repo --repo other/repo", False),
        ("gh pr create --body '-R other/repo' -R owner/repo", True),
    ],
)
def test_shell_repository_selectors_and_body_text(command, accepted):
    assert (
        bool(
            parse(
                "claude",
                "https://github.com/owner/repo/pull/9",
                name="bash",
                tool_args={"command": command},
            )
        )
        is accepted
    )


def test_opencode_retains_unsupported_creation_capture():
    spans = parse(
        "opencode",
        "https://github.com/owner/repo/pull/9",
        name="bash",
        tool_args={"command": "python create_pr.py && gh pr create"},
        return_spans=True,
    )
    assert any(s.attributes.get("pr.create.capture") == "unrecognized_creation_text" for s in spans)


@pytest.mark.parametrize("poll", [False, True])
def test_host_error_overrides_zero_exit_code(poll):
    from agentplay._core.ingestion.coding_agent.native_operations import ProcessEvidence

    evidence = ProcessEvidence()
    origin = {"name": "exec_command", "pr.create.provider": "github", "end_ts": "now"}
    if poll:
        evidence.observe(
            origin,
            {"cmd": "gh pr create"},
            {"session_id": 7, "exit_code": None, "wall_time_seconds": 0, "output": ""},
        )
    tool = {"name": "write_stdin", "end_ts": "later"} if poll else origin
    evidence.observe(
        tool,
        {"session_id": 7} if poll else {"cmd": "gh pr create"},
        {
            "isError": True,
            "exit_code": 0,
            "wall_time_seconds": 0,
            "output": "https://github.com/owner/repo/pull/9",
        },
    )
    assert origin["is_error"] is True
    assert origin["pr.create.output_url"] == ""
    assert evidence.pending == {}
