"""Retain Claude's cumulative cost checkpoints without inventing requests.

The process start clock is an identity, not a billing-window boundary: resumed
processes can carry costs from before it. Compare each checkpoint only with the
request observations preceding that checkpoint in this transcript.
"""

from collections.abc import Callable
import hashlib
import json
import math

from agentplay._core.ingestion.coding_agent.spans import model_id_or_none
from agentplay._core.pricing import normalize_model_key

TOKEN_FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def extract_cost_summary(
    lines: list[dict], *, request_expander: Callable[[dict], dict] | None = None
) -> dict | None:
    """Return bounded numeric evidence; flag malformed/oversized checkpoints.

    Snapshots are observations, never additive invoices. Keep the first copy of
    each checkpoint so replayed records cannot compare it with later activity.
    """
    if not any(line.get("type") == "cost-state" for line in lines):
        return None
    observations: dict[str, tuple[int, list[dict]]] = {}
    checkpoints: dict[str, dict] = {}
    invalid = 0
    for line in lines:
        message = line.get("message") or {}
        usage = message.get("usage") or {}
        if line.get("type") == "assistant" and message.get("id") and usage:
            observations.setdefault(message["id"], (0, []))[1].append(line)
        if line.get("type") != "cost-state":
            continue
        # Only evidence already visible at this checkpoint may participate.
        # A final fallback snapshot replaces earlier serving-only fragments;
        # expanding the whole transcript here would leak future evidence back.
        try:
            expanded = request_expander(observations) if request_expander else observations
            requests: dict[str, tuple[str, tuple[int, ...]]] = {}
            for _, fragments in expanded.values():
                for fragment in fragments:
                    message = fragment["message"]
                    usage = message["usage"]
                    model = normalize_model_key(model_id_or_none(message.get("model")) or "")
                    values = tuple(usage.get(field, 0) for field in USAGE_FIELDS)
                    if any(type(v) is not int or v < 0 for v in values):
                        continue  # The request parser owns malformed request handling.
                    old = requests.get(message["id"])
                    if old and old[0] and model and old[0] != model:
                        # A model-id rename, or a new id `normalize_model_key` folds
                        # differently, must discard this checkpoint, not the session.
                        raise ValueError("Conflicting model in Claude cost checkpoint")
                    model = model or (old[0] if old else "")
                    requests[message["id"]] = (
                        model,
                        tuple(max(a, b) for a, b in zip(old[1], values, strict=True))
                        if old
                        else values,
                    )
            models = line.get("modelUsage")
            start = line.get("startTime")
            total = line.get("totalCostUSD")
            unknown = line.get("hasUnknownModelCost")
            if (
                type(start) is not int
                or start < 0
                or type(unknown) is not bool
                or type(total) not in (float, int)
                or not math.isfinite(total)
                or total < 0
                or not isinstance(models, dict)
                or len(models) > 128
            ):
                raise ValueError("Invalid Claude cost-state checkpoint")
            identity = hashlib.sha256(
                json.dumps(
                    [start, total, unknown, models], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if identity in checkpoints:
                continue
            if len(checkpoints) >= 128:
                raise ValueError("Claude cost-state checkpoint limit exceeded")
            rows = []
            reported_models = set()
            for name, data in sorted(models.items()):
                if (
                    not isinstance(name, str)
                    or not name
                    or len(name) > 128
                    or not isinstance(data, dict)
                ):
                    raise ValueError("Invalid Claude cost-state model")
                values = [data.get(field) for field in TOKEN_FIELDS]
                cost = data.get("costUSD")
                if (
                    any(type(v) is not int or v < 0 for v in values)
                    or type(cost) not in (int, float)
                    or not math.isfinite(cost)
                    or cost < 0
                ):
                    raise ValueError("Invalid Claude cost-state usage")
                model = normalize_model_key(name)
                if model in reported_models:
                    raise ValueError("Duplicate normalized Claude checkpoint model")
                reported_models.add(model)
                visible = [sum(v[i] for m, v in requests.values() if m == model) for i in range(4)]
                rows.append(
                    {
                        "model": name,
                        "reported_cost_usd": cost,
                        "reported_tokens": values,
                        "visible_tokens": visible,
                    }
                )
            if not unknown and not math.isclose(
                sum(r["reported_cost_usd"] for r in rows), total, rel_tol=1e-9, abs_tol=1e-6
            ):
                raise ValueError("Claude checkpoint total disagrees with its models")
            checkpoints[identity] = {
                "source_hash": identity,
                "process_start_ms": start,
                "reported_total_usd": total,
                "has_unknown_model_cost": unknown,
                "models": rows,
                "visible_unreported_tokens": [
                    sum(v[i] for m, v in requests.values() if m not in reported_models)
                    for i in range(4)
                ],
            }
        except (ValueError, TypeError, OverflowError):
            invalid += 1
    return (
        {"checkpoints": list(checkpoints.values()), "invalid_checkpoints": invalid}
        if checkpoints or invalid
        else None
    )
