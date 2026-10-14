"""Real JSONL → billed components, with no network or provider calls."""

import json

import pytest

from agentplay._core.ingestion.coding_agent._sources.local.parsers.codex import parse_codex
from agentplay._core.ingestion.coding_agent.session_pure import build_session_metrics


def _line(kind, payload, second=0):
    return {"type": kind, "timestamp": f"2026-09-08T10:00:{second:02}Z", "payload": payload}


def _user(text="Fix the bug"):
    return _line(
        "response_item",
        {"role": "user", "content": [{"type": "input_text", "text": text}]},
    )


def _model(name):
    return _line("turn_context", {"model": name})


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


def _native_context(turn_id, model="gpt-5", second=0):
    return _line("turn_context", {"turn_id": turn_id, "model": model}, second)


def _native_record(
    response_id,
    turn_id,
    usage,
    cumulative,
    thread_id="native-thread",
    second=1,
    reasoning=0,
):
    def payload_values(values, reasoning_output_tokens=0):
        inp, cached, out = values
        return {
            "input_tokens": inp,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": 0,
            "output_tokens": out,
            "reasoning_output_tokens": reasoning_output_tokens,
            "total_tokens": inp + out,
        }

    return _line(
        "token_usage_record",
        {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "response_id": response_id,
            "usage": payload_values(usage, reasoning),
            "thread_token_usage": payload_values(cumulative),
        },
        second,
    )


def _parse(*records):
    data = b"\n".join(json.dumps(row).encode() for row in records) + b"\n"
    _, spans = parse_codex(data, trace_id="local:codex:billing-fixture")
    turns = [s for s in spans if s.name.startswith("Turn ")]
    return spans, turns


def test_reasoning_is_subset_and_cache_components_are_conserved():
    spans, (turn,) = _parse(_user(), _model("gpt-5"), _event(_usage(), _usage()))
    (event,) = turn.attributes["llm.cost_events"]
    assert turn.completion_tokens == event["output_tokens"] == 200
    assert event["input_tokens"] == 600
    assert event["cache_read_tokens"] == 400
    assert event["cache_write_5m_tokens"] == event["cache_write_1h_tokens"] == 0
    # reasoning_output_tokens=50 is folded into the 200 output_tokens above
    # (not billed on top), but is still surfaced separately for display.
    assert event["reasoning_tokens"] == 50
    assert turn.attributes["llm.token_count.reasoning"] == 50
    assert event["usage_basis"] == "last_token_usage"
    assert event["request_id"] is None
    assert event["message_id"].startswith("codex-usage:")
    assert event["source.kind"] == "jsonl"
    assert event["source.byte_offset"] > 0
    assert event["source.content_hash"]
    assert build_session_metrics(spans)["model_usage"] == [
        dict(
            model="gpt-5",
            requests=1,
            input_tokens=600,
            cache_read_tokens=400,
            cache_write_5m_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=200,
        )
    ]


def test_reasoning_tokens_are_clamped_to_the_output_they_are_a_subset_of():
    # Review-caught: a malformed vendor payload could report reasoning above
    # the snapshot's own output_tokens, contradicting the subset invariant
    # the whole field exists to display. Clamp rather than trust the vendor.
    _spans, (turn,) = _parse(
        _user(), _model("gpt-5"), _event(_usage(out=200, reasoning=9999), _usage())
    )
    (event,) = turn.attributes["llm.cost_events"]
    assert event["output_tokens"] == 200
    assert event["reasoning_tokens"] == 200
    assert turn.attributes["llm.token_count.reasoning"] == 200


@pytest.mark.parametrize("context_before_user", [True, False])
def test_model_switch_preserves_both_turn_and_request_models(context_before_user):
    first = [_user("first"), _model("gpt-5"), _event(_usage(), _usage())]
    boundary = [_model("gpt-5-mini"), _user("second")]
    if not context_before_user:
        boundary.reverse()
    spans, turns = _parse(*first, *boundary, _event(_usage(), _usage(2000, 800, 400), 2))
    assert [t.model_name for t in turns] == ["gpt-5", "gpt-5-mini"]
    assert [t.attributes["llm.cost_events"][0]["model"] for t in turns] == ["gpt-5", "gpt-5-mini"]
    assert [row["model"] for row in build_session_metrics(spans)["model_usage"]] == [
        "gpt-5",
        "gpt-5-mini",
    ]


def test_model_switch_inside_one_turn_keeps_each_usage_model():
    spans, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _event(_usage(), _usage()),
        _model("gpt-5-mini"),
        _event(_usage(), _usage(2000, 800, 400), 2),
    )
    assert [event["model"] for event in turn.attributes["llm.cost_events"]] == [
        "gpt-5",
        "gpt-5-mini",
    ]
    assert len(build_session_metrics(spans)["model_usage"]) == 2


def test_repeated_snapshots_and_old_records_do_not_repeat_usage():
    first = _event(_usage(), _usage())
    _, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        first,
        _event(_usage(), _usage(), 2),  # Same cumulative snapshot, later clock.
        _event(_usage(), _usage(2000, 800, 400), 3),
        first,  # Exact older record replayed after a newer snapshot.
    )
    assert turn.prompt_tokens == 2000
    assert turn.completion_tokens == 400
    assert len(turn.attributes["llm.cost_events"]) == 2


def test_observation_ids_survive_appends_and_prepended_nonusage_records():
    prefix = [_user(), _model("gpt-5"), _event(_usage(), _usage())]
    _, (before,) = _parse(*prefix)
    _, after = _parse(
        _line("session_meta", {"id": "native-id"}),
        *prefix,
        _user("another"),
        _event(_usage(), _usage(2000, 800, 400), 2),
    )
    expected = before.attributes["llm.cost_events"][0]
    actual = after[0].attributes["llm.cost_events"][0]
    assert actual["message_id"] == expected["message_id"]
    assert actual["source.content_hash"] == expected["source.content_hash"]
    assert actual["source.byte_offset"] != expected["source.byte_offset"]


def test_cumulative_only_usage_subtracts_each_component():
    spans, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _event(None, _usage()),
        _event(None, _usage(2500, 1000, 500, 100), 2),
    )
    first, second = turn.attributes["llm.cost_events"]
    assert first["usage_basis"] == second["usage_basis"] == "cumulative_delta"
    assert second["input_tokens"] == 900
    assert second["cache_read_tokens"] == 600
    assert second["output_tokens"] == 300
    # No per-call snapshot to read reasoning_output_tokens from on this
    # basis, and no baseline to delta it against either -- reported as 0
    # rather than a fabricated figure. See `_reasoning_tokens` callers.
    assert first["reasoning_tokens"] == second["reasoning_tokens"] == 0
    assert turn.attributes["llm.token_count.reasoning"] == 0
    metrics = build_session_metrics(spans)
    assert (metrics["input_fresh"], metrics["cache_read_tokens"], metrics["output_tokens"]) == (
        1500,
        1000,
        500,
    )


@pytest.mark.parametrize(
    "bad",
    [
        _usage(900, 1000),  # Cache cannot exceed input.
        _usage(inp=True),
        _usage(out=-1),
        {**_usage(), "cache_write_input_tokens": 100},  # No TTL evidence.
    ],
)
def test_bad_or_unpriceable_usage_rejects_capture(bad):
    # A vendor-drift or malformed record degrades this one usage snapshot --
    # not the whole transcript. Nothing gets captured or billed for it, and
    # the loss is surfaced via usage_complete rather than an aborted parse.
    spans, (turn,) = _parse(_user(), _model("gpt-5"), _event(None, bad))
    assert turn.attributes["llm.cost_events"] == []
    assert build_session_metrics(spans)["usage_complete"] is False


def test_cache_write_token_usage_costs_only_its_own_turn():
    """A nonzero cache_write_input_tokens (no TTL evidence) used to raise and
    lose every turn in the transcript. It must now cost only the turn that
    carried it -- every other turn's usage stays bit-identical."""
    good = _usage(1000, 400, 200)
    bad_cache_write = {**_usage(500, 100, 50), "cache_write_input_tokens": 64}
    spans, (turn_one, turn_two) = _parse(
        _user("first"), _model("gpt-5"), _event(good, good, second=1),
        _user("second"), _event(bad_cache_write, bad_cache_write, second=2),
    )
    # First turn's billing is untouched -- bit-identical to a session with no
    # cache-write component at all.
    assert (turn_one.prompt_tokens, turn_one.completion_tokens) == (1000, 200)
    assert len(turn_one.attributes["llm.cost_events"]) == 1
    # Second turn's cache-write record is rejected and dropped, never billed
    # and never allowed to abort the rest of the transcript.
    assert turn_two.attributes["llm.cost_events"] == []
    assert build_session_metrics(spans)["usage_complete"] is False


def test_a_rejected_record_does_not_poison_the_next_turn_billing():
    """Rejecting one snapshot must not double-bill or reject the turns after it.

    The baseline must advance along with the rejected record; otherwise the NEXT
    valid record would measure its delta from a total that still included the
    rejected record's tokens. The third turn below is the one that matters: it
    must neither bill the rejected 150 tokens a second time nor trip the
    backwards guard and take the rest of the session with it.
    """
    # Cumulative totals climb 1200, 1750, 2130 across the three records, so the
    # third record's delta is 380 and its own last_token_usage totals 380 too.
    # That only reconciles if the baseline moved to 1750 when the second record
    # was rejected; left at 1200 the delta reads 930 and the third turn is
    # rejected as well.
    spans, turns = _parse(
        _user("first"), _model("gpt-5"),
        _event(_usage(1000, 400, 200), _usage(1000, 400, 200), second=1),
        _user("second"),
        _event(
            {**_usage(500, 100, 50), "cache_write_input_tokens": 64},
            _usage(1500, 500, 250),
            second=2,
        ),
        _user("third"),
        _event(_usage(300, 100, 80), _usage(1800, 600, 330), second=3),
    )
    turn_one, turn_two, turn_three = turns
    assert (turn_one.prompt_tokens, turn_one.completion_tokens) == (1000, 200)
    assert turn_two.attributes["llm.cost_events"] == []
    # Recovering later billing is not the same as billing the session correctly:
    # the rejected turn's own usage is still missing, so the session has to say
    # so rather than report a total that looks whole.
    assert build_session_metrics(spans)["usage_complete"] is False
    # The third turn bills its own usage and nothing else.
    assert (turn_three.prompt_tokens, turn_three.completion_tokens) == (300, 80)
    assert len(turn_three.attributes["llm.cost_events"]) == 1


def test_a_backwards_snapshot_does_not_become_the_new_baseline():
    """The mirror of the test above: some records are rejected BECAUSE their own
    cumulative figures cannot be trusted, and those must never be adopted as the
    baseline. Here the counter drops to 800 and is rejected; the third record
    reports 1500, whose delta only reconciles against the trusted 1200. Adopting
    800 would make the delta 700 against a reported 300 and reject turn three
    too, one bad record silently ending billing for the rest of the session.
    """
    spans, turns = _parse(
        _user("first"), _model("gpt-5"),
        _event(_usage(1000, 400, 200), _usage(1000, 400, 200), second=1),
        _user("second"),
        _event(_usage(600, 200, 200), _usage(600, 200, 200), second=2),
        _user("third"),
        _event(_usage(220, 0, 80), _usage(1200, 400, 300), second=3),
    )
    turn_one, turn_two, turn_three = turns
    assert (turn_one.prompt_tokens, turn_one.completion_tokens) == (1000, 200)
    assert turn_two.attributes["llm.cost_events"] == []
    # Rejecting the record is only half the contract: the session must also say
    # its usage is incomplete. Without this, a parser that drops the record and
    # keeps billing would still pass, and the session would be reported as a
    # complete cost picture that is quietly missing a turn.
    assert build_session_metrics(spans)["usage_complete"] is False
    assert (turn_three.prompt_tokens, turn_three.completion_tokens) == (220, 80)


def test_backwards_components_do_not_become_the_new_baseline():
    """A record rejected for backwards COMPONENTS must not be adopted either.

    The total-level guards run before the baseline is remembered, but the
    component guard runs after, so a snapshot whose total climbs while its own
    components move backwards was recorded as the fallback baseline and then
    adopted by the handler that rejected it. Every later delta was then measured
    against figures the parser had already refused, which rejected the rest of
    the session: the exact one-bad-record-ends-billing failure the total-level
    guards exist to prevent.
    """
    spans, turns = _parse(
        _user("first"), _model("gpt-5"),
        _event(None, _usage(1000, 400, 200), second=1),
        _user("second"),
        # Total climbs 1200 -> 1600, but input drops 1000 -> 900: rejected.
        _event(None, _usage(900, 300, 700), second=2),
        _user("third"),
        # Reconciles against the trusted (1000, 400, 200), not the rejected one.
        _event(None, _usage(1400, 500, 600), second=3),
    )
    turn_one, turn_two, turn_three = turns
    assert (turn_one.prompt_tokens, turn_one.completion_tokens) == (1000, 200)
    assert turn_two.attributes["llm.cost_events"] == []
    assert (turn_three.prompt_tokens, turn_three.completion_tokens) == (400, 400)
    assert len(turn_three.attributes["llm.cost_events"]) == 1
    # The rejected turn's own usage is still missing, so the session says so.
    assert build_session_metrics(spans)["usage_complete"] is False


# Every `raise` in the token_count containment block, and what it means for the
# running cumulative baseline. Three separate MAJOR review findings were each one
# row of this table derived by hand, so the table itself is the test now: a
# rejection either leaves the baseline on the PREVIOUS record (correct only when
# this record's own total cannot be trusted) or moves it to THIS record (correct
# whenever the total is sound and the fault lies elsewhere). Getting it wrong in
# the "advances" direction is not one mis-billed turn: every later delta then
# spans two records, disagrees with its own `last`, and is rejected in turn, so
# one bad record silently ends billing for the rest of the session.
_BASELINE = _usage(1000, 400, 200)          # total 1200, the trusted baseline
_SOUND_SECOND = _usage(1500, 500, 250)      # total 1750, a well-formed successor

# (id, second record's cumulative, second record's last, baseline advances)
_REJECTION_TABLE = [
    # Sound total, unpriceable extra component: the figures are internally
    # consistent and the counter really did reach 1750.
    (
        "cache_write_is_unpriceable_not_malformed",
        {**_SOUND_SECOND, "cache_write_input_tokens": 64},
        _usage(500, 100, 50),
        True,
    ),
    # Sound total, the fault is in `last`, which the baseline does not depend on.
    ("last_disagrees_with_delta", _SOUND_SECOND, _usage(1, 0, 1), True),
    # The total itself is unreadable, so nothing downstream of it can be trusted.
    ("cumulative_total_is_not_an_int", {**_SOUND_SECOND, "total_tokens": "x"}, _usage(500, 100, 50), False),
    ("cumulative_total_is_negative", {**_SOUND_SECOND, "total_tokens": -5}, _usage(500, 100, 50), False),
    # The snapshot contradicts itself: components malformed, or not adding up.
    ("component_is_not_an_int", {**_SOUND_SECOND, "input_tokens": "x"}, _usage(500, 100, 50), False),
    ("cached_exceeds_input", {**_SOUND_SECOND, "cached_input_tokens": 9999}, _usage(500, 100, 50), False),
    ("total_is_not_input_plus_output", {**_SOUND_SECOND, "total_tokens": 99999}, _usage(500, 100, 50), False),
    # A counter that went down is a reset or a stale read; adopting it would
    # reject every later record for moving backwards against it.
    ("cumulative_moved_backwards", _usage(800, 300, 100), _usage(500, 100, 50), False),
]


@pytest.mark.parametrize(
    ("cumulative", "last", "baseline_advances"),
    [pytest.param(cum, lst, adv, id=name) for name, cum, lst, adv in _REJECTION_TABLE],
)
def test_a_rejected_record_advances_the_baseline_only_when_its_total_is_sound(
    cumulative, last, baseline_advances
):
    """One fixture, one mutation per rejection reason, one assertion: turn three.

    The third record's `last` reports 380 and its cumulative reads 2130. That
    reconciles against a baseline of 1750 (2130 - 1750 = 380) and against
    nothing else, so turn three billing its real 300/80 is a direct readout of
    whether the rejected second record advanced the baseline.
    """
    spans, turns = _parse(
        _user("first"), _model("gpt-5"),
        _event(_usage(1000, 400, 200), _BASELINE, second=1),
        _user("second"),
        _event(last, cumulative, second=2),
        _user("third"),
        _event(_usage(300, 100, 80), _usage(1800, 600, 330), second=3),
    )
    turn_one, turn_two, turn_three = turns
    # Turn one is untouched and turn two loses its own usage either way.
    assert (turn_one.prompt_tokens, turn_one.completion_tokens) == (1000, 200)
    assert turn_two.attributes["llm.cost_events"] == []
    expected = (300, 80) if baseline_advances else (0, 0)
    assert (turn_three.prompt_tokens, turn_three.completion_tokens) == expected
    # Whichever way it went, the session never claims a complete cost picture.
    assert build_session_metrics(spans)["usage_complete"] is False


def test_a_repeated_total_carrying_an_unpriceable_component_is_still_refused():
    """The repeated-total short circuit must not skip the priceability check.

    A snapshot whose total simply has not moved is normally nothing to do:
    the block `continue`s without billing and without touching the baseline.
    But "nothing to bill" is not the same as "nothing to say". If that same
    record carries a cache write this parser cannot price, the session has
    seen a component it did not account for, and continuing silently makes it
    report a complete cost picture that is not complete.
    """
    repeated = _usage(1000, 400, 200)
    spans, turns = _parse(
        _user("first"), _model("gpt-5"),
        _event(repeated, repeated, second=1),
        _user("second"),
        _event(repeated, {**repeated, "cache_write_input_tokens": 64}, second=2),
    )
    turn_one, turn_two = turns
    # The first record still bills normally; the repeated one adds nothing.
    assert (turn_one.prompt_tokens, turn_one.completion_tokens) == (1000, 200)
    assert (turn_two.prompt_tokens, turn_two.completion_tokens) == (0, 0)
    assert build_session_metrics(spans)["usage_complete"] is False


def test_decreasing_cumulative_snapshot_is_not_guessed_as_new_cost():
    spans, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _event(_usage(), _usage(2000, 800, 400)),
        _event(_usage(), _usage(), 2),
    )
    # The first snapshot still bills; the backwards-moving second one is
    # rejected and dropped rather than guessed at or aborting the transcript.
    assert len(turn.attributes["llm.cost_events"]) == 1
    assert build_session_metrics(spans)["usage_complete"] is False


def test_cumulative_only_delta_requires_known_previous_components():
    spans, (turn,) = _parse(
        _user(), _model("gpt-5"),
        _event(_usage(), {"total_tokens": 1200}),
        _event(None, _usage(2000, 800, 400), 2),
    )
    assert len(turn.attributes["llm.cost_events"]) == 1
    assert build_session_metrics(spans)["usage_complete"] is False


def test_valid_zero_usage_and_rate_limit_only_turns_have_empty_evidence():
    spans, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _event(None, None),
        _event(_usage(0, 0, 0, 0), _usage(0, 0, 0, 0), 2),
    )
    assert turn.attributes["llm.cost_events"] == []
    assert build_session_metrics(spans)["model_usage"] == []


@pytest.mark.parametrize("usage_kind", ["last", "cumulative"])
@pytest.mark.parametrize("total", [True, None, "0", 0.0, -1])
def test_supplied_total_must_match_consumed_components(usage_kind, total):
    bad = {**_usage(0, 0, 0, 0), "total_tokens": total}
    last, cumulative = (bad, None) if usage_kind == "last" else (None, bad)
    spans, (turn,) = _parse(_user(), _model("gpt-5"), _event(last, cumulative))
    assert turn.attributes["llm.cost_events"] == []
    assert build_session_metrics(spans)["usage_complete"] is False


def test_last_usage_can_omit_redundant_total():
    last = _usage()
    del last["total_tokens"]
    _, (turn,) = _parse(_user(), _model("gpt-5"), _event(last, None))
    assert (turn.prompt_tokens, turn.completion_tokens) == (1000, 200)
    assert len(turn.attributes["llm.cost_events"]) == 1


def test_unchanged_cumulative_ignores_unconsumed_last_usage_placeholder():
    placeholder = {**_usage(0, 0, 0, 0), "total_tokens": 76776}
    _, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _event(_usage(), _usage()),
        _event(placeholder, _usage(), 2),
        _event(_usage(), _usage(2000, 800, 400), 3),
    )
    assert (turn.prompt_tokens, turn.completion_tokens) == (2000, 400)
    assert len(turn.attributes["llm.cost_events"]) == 2


def test_native_ledger_survives_legacy_reset_and_attaches_delayed_turn():
    rows = [
        _line("session_meta", {"id": "native-thread"}),
        _native_context("turn-1", second=0),
        _user(
            "first",
        ),
        _native_record("response-1", "turn-1", (1000, 400, 200), (1000, 400, 200), second=1),
        _line("compacted", {}, 2),
        # Codex can flush this native record before its context and user line.
        _native_record("response-2", "turn-2", (500, 100, 50), (1500, 500, 250), second=3),
        _native_context("turn-2", model="gpt-5-mini", second=4),
        _user(
            "second",
        ),
        # Legacy display counters are advisory once native records exist.
        _event({"input_tokens": 1}, {}, 5),
    ]
    _, spans = _parse(*rows)
    turns = [span for span in spans if span.name.startswith("Turn ")]
    events = [event for turn in turns for event in turn.attributes["llm.cost_events"]]
    assert [(event["message_id"], event["model"]) for event in events] == [
        ("codex-response:response-1", "gpt-5"),
        ("codex-response:response-2", "gpt-5-mini"),
    ]
    assert [
        turn.attributes["llm.cost_events"][0]["turn_id"]
        for turn in turns
        if turn.attributes["llm.cost_events"]
    ] == [
        "turn-1",
        "turn-2",
    ]
    assert build_session_metrics(spans)["model_usage"] == [
        dict(
            model="gpt-5",
            requests=1,
            input_tokens=600,
            cache_read_tokens=400,
            cache_write_5m_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=200,
        ),
        dict(
            model="gpt-5-mini",
            requests=1,
            input_tokens=400,
            cache_read_tokens=100,
            cache_write_5m_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=50,
        ),
    ]


def test_native_duplicate_response_is_emitted_once():
    record = _native_record("response-1", "turn-1", (1000, 400, 200), (1000, 400, 200))
    _, (turn,) = _parse(
        _line("session_meta", {"id": "native-thread"}),
        _native_context("turn-1"),
        _user(),
        record,
        record,
    )
    assert [event["message_id"] for event in turn.attributes["llm.cost_events"]] == [
        "codex-response:response-1"
    ]


def test_native_ledger_reasoning_tokens_are_read_per_response():
    record = _native_record(
        "response-1", "turn-1", (1000, 400, 200), (1000, 400, 200), reasoning=75
    )
    _, (turn,) = _parse(
        _line("session_meta", {"id": "native-thread"}),
        _native_context("turn-1"),
        _user(),
        record,
    )
    (event,) = turn.attributes["llm.cost_events"]
    assert event["reasoning_tokens"] == 75
    assert turn.attributes["llm.token_count.reasoning"] == 75


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda row: row["payload"].update(thread_id="other"), "owner"),
        (lambda row: row["payload"].update(response_id=""), "response_id"),
        (
            lambda row: row["payload"].update(
                thread_token_usage={
                    "input_tokens": 2000,
                    "cached_input_tokens": 400,
                    "output_tokens": 200,
                    "total_tokens": 2200,
                }
            ),
            "gap",
        ),
    ],
)
def test_native_ledger_rejects_unowned_or_incomplete_records(mutate, message):
    record = _native_record("response-1", "turn-1", (1000, 400, 200), (1000, 400, 200))
    mutate(record)
    # The whole native ledger is untrustworthy once one record fails
    # validation, so the session degrades to no native usage instead of
    # aborting the transcript with a raise.
    spans, turns = _parse(_line("session_meta", {"id": "native-thread"}), record)
    assert turns == []
    assert spans[0].attributes["llm.usage_complete"] is False


def test_native_conflicting_replay_is_rejected():
    first = _native_record("response-1", "turn-1", (1000, 400, 200), (1000, 400, 200))
    second = _native_record("response-1", "turn-1", (1001, 400, 200), (1001, 400, 200), second=2)
    spans, turns = _parse(_line("session_meta", {"id": "native-thread"}), first, second)
    assert turns == []
    assert spans[0].attributes["llm.usage_complete"] is False


def test_last_usage_that_disagrees_with_the_cumulative_delta_rejects_capture():
    """The snapshot says the counter moved by 1,200 but the
    per-call components add up to 600 -- billing would disagree with the
    vendor's own total, so the capture is rejected rather than trusted."""
    first = _usage(1000, 400, 200)
    spans, (turn,) = _parse(
        _user(),
        _model("gpt-5"),
        _event(first, first, second=1),
        _event(_usage(500, 0, 100), _usage(2000, 400, 400), second=2),
    )
    # The first snapshot still bills; the disagreeing second one is rejected
    # and dropped rather than trusted or aborting the transcript.
    assert len(turn.attributes["llm.cost_events"]) == 1
    assert build_session_metrics(spans)["usage_complete"] is False


def test_last_usage_that_matches_the_cumulative_delta_is_accepted():
    first = _usage(1000, 400, 200)
    spans, _turns = _parse(
        _user(),
        _model("gpt-5"),
        _event(first, first, second=1),
        _event(_usage(1000, 0, 200), _usage(2000, 400, 400), second=2),
    )
    events = [e for s in spans for e in s.attributes.get("llm.cost_events", [])]
    assert len(events) == 2
