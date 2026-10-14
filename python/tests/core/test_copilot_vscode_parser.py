"""Copilot in VS Code (Chat / agent mode) parser.

Fixtures below are SYNTHETIC, shaped to match what real local
`workspaceStorage/*/chatSessions/*.jsonl` and `.json` files carry (see the parser's
module docstring). No real prompt/response text
appears in this file; fixture strings are placeholders only.

Each test pins one gap this parser exists to close (see the module
docstring's "Two on-disk shapes" / "Known gap" sections):

1. A `.json` snapshot (already-final state) parses directly.
2. A `.jsonl` file (kind-0 snapshot + kind-1/2 patches) replays to the SAME
   result a `.json` sibling with equivalent content would give -- including a
   kind-2 splice WITH an explicit `i` (streamed response growth) and WITHOUT
   one (plain append), both seen in real files.
3. Tool names alias through `copilot-vscode`'s table; `arguments` (a
   JSON-encoded STRING in real files, not a dict) is decoded before use.
4. Model resolution skips a `capi-*` routing id and the `copilot/auto` router
   alias from `modelId`, in that fallback order.
5. `result.usage.promptTokens` is preferred over `result.metadata`'s sparser
   fields.
6. A request-level `errorDetails` marks the turn ERROR; no error signal
   fabricated when tool results are merely absent.
7. `filePath` (camelCase) survives into the `file_path` key the shared
   extractors match.
"""

from __future__ import annotations

import json

from agentplay._core.ingestion.coding_agent._sources.local.parsers.copilot_vscode import (
    parse_copilot_vscode,
)


def _tool_call(call_id: str, name: str, **args) -> dict:
    return {"id": call_id, "name": name, "arguments": json.dumps(args)}


def _request(
    request_id: str,
    *,
    ts_ms: int,
    model_id: str = "copilot/auto",
    resolved_model: str | None = None,
    usage: dict | None = None,
    metadata_tokens: dict | None = None,
    tool_rounds: list[dict] | None = None,
    tool_results: dict | None = None,
    error: dict | None = None,
    elapsed_ms: int = 1000,
) -> dict:
    metadata = {
        "toolCallRounds": tool_rounds or [],
        "toolCallResults": tool_results or {},
        **(metadata_tokens or {}),
    }
    if resolved_model is not None:
        metadata["resolvedModel"] = resolved_model
    result = {"metadata": metadata}
    if usage is not None:
        result["usage"] = usage
    if error is not None:
        result["errorDetails"] = error
    return {
        "requestId": request_id,
        "timestamp": ts_ms,
        "elapsedMs": elapsed_ms,
        "isComplete": True,
        "modelId": model_id,
        "result": result,
    }


def _snapshot_line(state: dict) -> bytes:
    return (json.dumps({"kind": 0, "v": state}) + "\n").encode()


def _set_line(path: list, value) -> bytes:
    return (json.dumps({"kind": 1, "k": path, "v": value}) + "\n").encode()


def _splice_line(path: list, value: list, index: int | None = None) -> bytes:
    obj: dict = {"kind": 2, "k": path, "v": value}
    if index is not None:
        obj["i"] = index
    return (json.dumps(obj) + "\n").encode()


def test_a_json_snapshot_parses_directly():
    state = {
        "sessionId": "s1",
        "creationDate": 1_780_000_000_000,
        "requests": [_request("r1", ts_ms=1_780_000_001_000, resolved_model="gpt-5.4")],
    }
    trace, spans = parse_copilot_vscode(json.dumps(state).encode(), trace_id="local:x:s1")
    turns = [s for s in spans if s.name == "Turn 1"]
    assert len(turns) == 1
    assert turns[0].attributes["llm.model_name"] == "gpt-5.4"
    root = next(s for s in spans if s.parent_id is None)
    assert root.attributes["session.id"] == "s1"


def test_jsonl_replay_matches_the_equivalent_json_document():
    final = {
        "sessionId": "s2",
        "creationDate": 1_780_000_000_000,
        "requests": [_request("r1", ts_ms=1_780_000_001_000, resolved_model="grok-code-fast-1")],
    }
    lines = [
        _snapshot_line({"sessionId": "s2", "creationDate": 1_780_000_000_000, "requests": []}),
        _splice_line(["requests"], [final["requests"][0]]),  # kind 2, no "i": append
    ]
    json_trace, json_spans = parse_copilot_vscode(json.dumps(final).encode(), trace_id="j")
    jsonl_trace, jsonl_spans = parse_copilot_vscode(b"".join(lines), trace_id="j")
    assert len(jsonl_spans) == len(json_spans)
    assert [s.attributes.get("llm.model_name") for s in jsonl_spans if s.name == "Turn 1"] == [
        s.attributes.get("llm.model_name") for s in json_spans if s.name == "Turn 1"
    ]


def test_a_streamed_response_growth_patch_with_explicit_index_is_applied():
    # Real shape: kind-2 patches on requests[N].response with a rising "i" --
    # each one truncates to "i" items then appends, simulating token-by-token
    # streaming. This must not raise or silently drop the request.
    lines = [
        _snapshot_line(
            {"sessionId": "s3", "requests": [_request("r1", ts_ms=1, elapsed_ms=1)]}
        ),
        _set_line(["requests", 0, "response"], []),
        _splice_line(["requests", 0, "response"], [{"value": "a"}]),
        _splice_line(["requests", 0, "response"], [{"value": "a"}, {"value": "b"}], index=0),
    ]
    trace, spans = parse_copilot_vscode(b"".join(lines), trace_id="j3")
    turn = next(s for s in spans if s.name == "Turn 1")
    # Strong assertion (not merely that the request survived; that the indexed
    # splice actually reconstructed correctly): "b" replaced the SECOND item at i=0 rather than appending a
    # third, and both are real response text now that output.value is wired.
    assert turn.attributes["output.value"] == "a\nb"


def test_malformed_patches_are_skipped_not_fatal():
    lines = [
        _snapshot_line({"sessionId": "s4", "requests": [_request("r1", ts_ms=1)]}),
        b"not json at all\n",
        _set_line(["requests", 99, "nope"], "x"),  # out-of-range index
        _splice_line(["missing", "path"], ["x"]),  # path doesn't exist
    ]
    trace, spans = parse_copilot_vscode(b"".join(lines), trace_id="j4")
    assert any(s.name == "Turn 1" for s in spans)


def test_tool_names_alias_and_arguments_string_is_decoded():
    rounds = [
        {
            "toolCalls": [
                _tool_call("c1", "run_in_terminal", command="git log -1"),
                _tool_call("c2", "replace_string_in_file", filePath="/repo/a.py", oldString="\nx\n", newString="\ny\n"),
            ]
        }
    ]
    results = {"c1": {"content": [{"value": "abc123"}]}, "c2": {"content": [{"value": "ok"}]}}
    req = _request("r1", ts_ms=1, resolved_model="gpt-5.4", tool_rounds=rounds, tool_results=results)
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s5", "requests": [req]}).encode(), trace_id="j5"
    )
    tools = {s.attributes["tool.name"]: s for s in spans if s.span_kind == "TOOL"}
    assert set(tools) == {"Bash", "Edit"}
    assert tools["Bash"].attributes["input.value"] == '{"command": "git log -1"}'
    assert tools["Bash"].status_code == "OK"
    # file_path (snake_case) was inserted ahead of the camelCase filePath.
    assert '"file_path": "/repo/a.py"' in tools["Edit"].attributes["input.value"]


def test_a_tool_call_with_no_result_is_unset_not_an_error():
    rounds = [{"toolCalls": [_tool_call("c1", "read_file", filePath="/a.py")]}]
    req = _request("r1", ts_ms=1, tool_rounds=rounds, tool_results={})  # never resolved
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s6", "requests": [req]}).encode(), trace_id="j6"
    )
    tool = next(s for s in spans if s.span_kind == "TOOL")
    assert tool.status_code == "UNSET"


def test_model_resolution_skips_capi_and_the_router_alias_in_order():
    real = _request("r1", ts_ms=1, model_id="copilot/auto", resolved_model="grok-code-fast-1")
    capi = _request("r2", ts_ms=2, model_id="copilot/auto", resolved_model="capi-noe-ptuc-h200")
    neither = _request("r3", ts_ms=3, model_id="copilot/auto", resolved_model=None)
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s7", "requests": [real, capi, neither]}).encode(),
        trace_id="j7",
    )
    turns = [s for s in spans if s.name.startswith("Turn")]
    assert [t.attributes["llm.model_name"] for t in turns] == [
        "grok-code-fast-1",
        "copilot/auto",  # capi-* skipped -> falls back to the router alias
        "copilot/auto",  # no resolvedModel at all -> same fallback
    ]


def test_usage_promptTokens_is_preferred_over_metadata():
    req = _request(
        "r1",
        ts_ms=1,
        usage={"promptTokens": 41236, "completionTokens": 2027},
        metadata_tokens={"promptTokens": 999, "outputTokens": 999},
    )
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s8", "requests": [req]}).encode(), trace_id="j8"
    )
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.attributes["llm.token_count.prompt"] == 41236
    assert turn.attributes["llm.token_count.completion"] == 2027


def test_metadata_tokens_used_when_usage_is_absent():
    req = _request("r1", ts_ms=1, metadata_tokens={"promptTokens": 52214, "outputTokens": 1220})
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s9", "requests": [req]}).encode(), trace_id="j9"
    )
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.attributes["llm.token_count.prompt"] == 52214
    assert turn.attributes["llm.token_count.completion"] == 1220


def test_a_request_level_error_marks_the_turn_error_and_carries_the_code():
    req = _request(
        "r1",
        ts_ms=1,
        error={"code": "quotaExceeded", "message": "You've reached your monthly quota."},
    )
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s10", "requests": [req]}).encode(), trace_id="j10"
    )
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.status_code == "ERROR"
    assert turn.attributes["error.code"] == "quotaExceeded"


def test_no_error_is_fabricated_when_a_request_has_no_errorDetails():
    req = _request("r1", ts_ms=1)
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s11", "requests": [req]}).encode(), trace_id="j11"
    )
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.status_code == "OK"
    assert "error.code" not in turn.attributes


def test_an_empty_session_produces_no_spans_but_does_not_raise():
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s12", "requests": []}).encode(), trace_id="j12"
    )
    assert spans == []


def test_root_span_carries_the_session_id_and_agent_tool():
    req = _request("r1", ts_ms=1_780_000_000_000)
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s13", "requests": [req]}).encode(),
        trace_id="j13",
        user_id="u-42",
        project_name="my-repo",
    )
    root = next(s for s in spans if s.parent_id is None)
    assert root.attributes["session.id"] == "s13"
    assert root.attributes["session.agent_tool"] == "copilot-vscode"
    assert root.attributes["project.name"] == "my-repo"
    # Review-caught: user_id was accepted but never attached to the root span,
    # breaking the TraceSource contract every sibling parser follows.
    assert root.attributes["user.id"] == "u-42"


def test_root_span_user_id_defaults_to_empty_string():
    req = _request("r1", ts_ms=1)
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s14", "requests": [req]}).encode(), trace_id="j14"
    )
    root = next(s for s in spans if s.parent_id is None)
    assert root.attributes["user.id"] == ""


def test_turn_input_value_comes_from_rendered_user_message_text_parts_only():
    # Real shape: a list of parts; only type==1 parts carry a string "text".
    # type 2/3 (image / other reference) never do and must be skipped, not
    # stringified into noise.
    req = _request("r1", ts_ms=1)
    req["result"]["metadata"]["renderedUserMessage"] = [
        {"type": 1, "text": "fix the bug"},
        {"type": 3, "imageUrl": "data:..."},
        {"type": 1, "text": "in auth.py"},
    ]
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s15", "requests": [req]}).encode(), trace_id="j15"
    )
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.attributes["input.value"] == "fix the bug\nin auth.py"


def test_turn_output_value_excludes_thinking_and_tool_marker_parts():
    # Real shape: response is heterogeneous (thinking, toolInvocationSerialized,
    # mcpServersStarting, ...); only the markdown/text parts -- no "kind" key at
    # all, a string "value" -- are the actual answer.
    req = _request("r1", ts_ms=1)
    req["response"] = [
        {"kind": "thinking", "value": "let me consider..."},
        {"kind": "mcpServersStarting", "didStartServerIds": ["s1"]},
        {"value": "Here is the fix."},
    ]
    trace, spans = parse_copilot_vscode(
        json.dumps({"sessionId": "s16", "requests": [req]}).encode(), trace_id="j16"
    )
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.attributes["output.value"] == "Here is the fix."


def test_model_falls_through_to_the_session_level_selected_model():
    # Real shape: state.inputState.selectedModel.identifier, session-scoped not
    # per-request -- the third tier the module docstring already promised but
    # the code never implemented.
    req = _request("r1", ts_ms=1, model_id="copilot/auto", resolved_model=None)
    state = {
        "sessionId": "s17",
        "requests": [req],
        "inputState": {"selectedModel": {"identifier": "claude-sonnet-5"}},
    }
    trace, spans = parse_copilot_vscode(json.dumps(state).encode(), trace_id="j17")
    turn = next(s for s in spans if s.name == "Turn 1")
    assert turn.attributes["llm.model_name"] == "claude-sonnet-5"


def test_session_level_model_is_skipped_when_itself_a_routing_alias():
    req = _request("r1", ts_ms=1, model_id="copilot/auto", resolved_model=None)
    state = {
        "sessionId": "s18",
        "requests": [req],
        "inputState": {"selectedModel": {"identifier": "capi-noe-ptuc-h200"}},
    }
    trace, spans = parse_copilot_vscode(json.dumps(state).encode(), trace_id="j18")
    turn = next(s for s in spans if s.name == "Turn 1")
    # Falls back further to the per-request modelId (still "copilot/auto"),
    # never a bare capi-* id.
    assert turn.attributes["llm.model_name"] == "copilot/auto"
