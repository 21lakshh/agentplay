"""token-count coercion must survive an explicit JSON null.

Some parsers (e.g. OpenCode) preserve `null` for a token attribute rather than
omitting the key, so `dict.get(key, 0)` returns None and a bare `int(None)` used
to raise, aborting the whole session's aggregation. The coercion sites now read
`int(x.attributes.get(key) or 0)`, so an explicit null degrades to 0.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agentplay._core.ingestion.coding_agent.session_pure import _context_growth
from agentplay._core.ingestion.coding_agent.spans import SpanData


def _turn_span(turn: int, prompt_tokens) -> SpanData:
    now = datetime.now(UTC)
    return SpanData(
        span_id=f"s{turn}",
        trace_id="t",
        parent_id="root",
        name=f"Turn {turn}",
        span_kind="LLM",
        status_code="OK",
        start_time=now,
        end_time=now,
        attributes={"llm.token_count.prompt": prompt_tokens},
    )


def test_explicit_null_prompt_tokens_coerces_to_zero():
    # The key is PRESENT with value None (an explicit JSON null), the case that
    # used to raise TypeError on int(None).
    rows = _context_growth([_turn_span(1, None)])
    assert rows == [{"turn": 1, "prompt_tokens": 0}]


def test_real_token_value_passes_through():
    rows = _context_growth([_turn_span(1, 1234), _turn_span(2, 0)])
    assert rows == [{"turn": 1, "prompt_tokens": 1234}, {"turn": 2, "prompt_tokens": 0}]
