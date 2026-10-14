"""Legacy totals preserve activity; a missing billing split is never a price."""

import json

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex
from agentplay._core.ingestion.coding_agent.session_pure import build_session_metrics


def _line(kind, payload, second=0):
    return {"type": kind, "timestamp": f"2026-09-13T00:00:{second:02}Z", "payload": payload}


def _user():
    return _line("event_msg", {"type": "user_message", "message": "inspect"})


def _model(model):
    return _line("turn_context", {"model": model})


def _usage(inp=1000, cached=400, out=200, reasoning=50):
    return dict(
        input_tokens=inp,
        cached_input_tokens=cached,
        output_tokens=out,
        reasoning_output_tokens=reasoning,
        total_tokens=inp + out,
    )


def _event(last, cumulative, second=1):
    return _line(
        "event_msg",
        {
            "type": "token_count",
            "info": {"last_token_usage": last, "total_token_usage": cumulative},
        },
        second,
    )


def _parse(*rows):
    _, spans = parse_codex(
        "\n".join(map(json.dumps, rows)).encode(), trace_id="local:codex:total-only"
    )
    return spans, [s for s in spans if s.name.startswith("Turn ")]


@pytest.mark.parametrize("zeros", [False, True])
def test_total_only_preserves_turn_and_tool_without_minting_requests(zeros):
    total = {**(_usage(0, 0, 0, 0) if zeros else {}), "total_tokens": 15585}
    spans, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _line(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": '{"cmd":"pwd"}',
            },
        ),
        _line(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Process exited with code 0",
            },
        ),
        _event(total, total),
    )
    assert turn.attributes["llm.cost_events"] == []
    assert len([s for s in spans if s.attributes.get("tool.name")]) == 1
    metrics = build_session_metrics(spans)
    assert metrics["usage_complete"] is False
    assert metrics["usage_state"] == "unknown_components"
    assert metrics["reported_total_tokens"] == 15585
    assert metrics["model_usage"] == []
    assert "retry_waste_marginal_usd" not in metrics


def test_total_only_gap_preserves_known_requests_without_claiming_complete_coverage():
    total = {"total_tokens": 1500}
    spans, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _event(_usage(), _usage()),
        _event(total, total, 2),
        _event(_usage(), _usage(2200, 800, 500), 3),
    )
    assert len(turn.attributes["llm.cost_events"]) == 2
    assert build_session_metrics(spans)["usage_complete"] is False


def test_nonzero_conflicting_components_still_fail():
    # A conflicting total is still never trusted -- it just costs this one
    # snapshot instead of the whole transcript.
    spans, (turn,) = _parse(_user(), _event(None, {**_usage(), "total_tokens": 9000}))
    assert turn.attributes["llm.cost_events"] == []
    assert build_session_metrics(spans)["usage_complete"] is False


def test_repaired_source_becomes_complete_without_sticky_unknown_state():
    before, _ = _parse(_user(), _model("gpt-5"), _event(None, {"total_tokens": 1200}))
    after, _ = _parse(_user(), _model("gpt-5"), _event(_usage(), _usage()))
    assert build_session_metrics(before)["usage_complete"] is False
    assert "usage_complete" not in build_session_metrics(after)
    assert len(build_session_metrics(after)["model_usage"]) == 1
