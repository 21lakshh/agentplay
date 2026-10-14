"""Universal transcript payload shapes.

Every coding-agent source (local ~/.claude file walks,
and so on) normalizes its raw transcript into this single
shape. Extractors only ever see `SpanData` lists — they don't know
which source produced them.

This module does NOT carry the `TraceSource` Protocol. The Protocol lives in
`_sources/_base.py` so it stays alongside the per-source adapters and
out of the extractor import path. Splitting them keeps the dependency
direction clean — extractors import `spans`, sources import `spans`,
sources additionally import `_sources/_base`.

# Why dataclasses, not Pydantic

These shapes flow inside a single extractor call. They're not
persisted, not validated at API boundaries, and not exposed through
JSON. `@dataclass` is lighter and keeps the parsers simple.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re

_SENTINEL_MODEL_IDS: frozenset[str] = frozenset(
    {"default", "default_routing", "unknown", "none", "n/a"}
)
_STRIP_SENTINEL_SUFFIX_RE = re.compile(
    r"\s*\((?:default|default_routing|unknown|none|n/a)\)$", re.IGNORECASE
)


def model_id_or_none(raw: object) -> str | None:
    """Return a trustworthy model identifier, or ``None`` for a sentinel.

    Transcript formats occasionally emit internal placeholders such as
    ``<synthetic>`` instead of the model that generated a user-visible turn.
    They are not model identifiers and must not become model buckets in
    per-model views or pricing. This deliberately does not
    guess aliases: a real provider model id is preserved exactly.
    """
    if not isinstance(raw, str):
        return None
    model_id = raw.strip()
    if not model_id:
        return None
    if model_id.startswith("<") and model_id.endswith(">"):
        return None
    if model_id.startswith("(") and model_id.endswith(")"):
        return None
    # Strip trailing parenthetical sentinels so composite strings like
    # "claude-sonnet-4-6 (default_routing)" resolve to the real model.
    model_id = _STRIP_SENTINEL_SUFFIX_RE.sub("", model_id).strip()
    if not model_id:
        return None
    if model_id.lower() in _SENTINEL_MODEL_IDS:
        return None
    return model_id


def last_genuine_model_id(spans: list[SpanData]) -> str | None:
    """The most recent real model id in a span list, ignoring placeholders.

    Claude Code stamps ``<synthetic>`` on assistant entries the CLI wrote
    itself rather than the model — API-error notices, rate-limit lines,
    "Not logged in", "No response requested" — all carrying zero usage. A
    session that ends on one of those reads as ``<synthetic>`` even though
    its real turns burned real tokens, and a placeholder cannot be priced.

    Two different kinds of transcript land on that same label:

      * Some hold real turns alongside the error lines and carry essentially
        all of the prompt tokens. A genuine model ran; this helper finds it
        and the cost becomes knowable.
      * Others carry **zero** prompt tokens: the session opened, hit an error
        or a login prompt, and nothing ever ran. There is no genuine model to
        find, so the cost stays unknown, which is correct: they cost nothing.

    Walks backwards because the model a session ended on is the best single
    label for it.

    Args:
        spans: the session's parsed spans, in transcript order.

    Returns:
        The last span-level model id that survives :func:`model_id_or_none`,
        or ``None`` when the session carries no genuine model at all (which
        is honest: the cost stays null rather than being priced at a guess).

    Example:
        >>> last_genuine_model_id([turn_on_opus, api_error_span])
        'claude-opus-4-8'
    """
    for span in reversed(spans):
        model_id = model_id_or_none(span.model_name)
        if model_id:
            return model_id
    return None


# Ordered most-specific-first: "API Error: 402 Router balance depleted" must
# resolve to the balance reason, not the generic api_error catch-all below it.
# Strings are verbatim from Claude-Code-written ``<synthetic>`` entries.
_FAILED_START_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("model_unavailable", re.compile(r"issue with the selected model", re.I)),
    ("router_balance_depleted", re.compile(r"router balance depleted|top up credits", re.I)),
    ("credentials_missing", re.compile(r"could not load credentials", re.I)),
    ("org_policy_block", re.compile(r"organization has disabled claude", re.I)),
    ("prompt_too_long", re.compile(r"prompt is too long", re.I)),
    (
        "not_logged_in",
        re.compile(r"not logged in|please run /login|invalid api key", re.I),
    ),
    (
        "rate_limited",
        re.compile(r"rate limit|usage limit reached|hit your usage limit", re.I),
    ),
    ("api_error", re.compile(r"api error", re.I)),
)


def failed_start_reason(spans: list[SpanData]) -> str:
    """Name the startup failure recorded in an error-only session's spans.

    A "failed start" is a session where no model ever ran: every assistant
    entry is a CLI-written ``<synthetic>`` notice and token usage is zero.
    The caller owns that guard (it already has the aggregated
    totals); this function only names the reason from the error text the
    CLI wrote, which the parser preserves in each turn span's
    ``output.value`` attribute.

    Typical reasons are ``model_unavailable`` (a harness pinned to an
    inaccessible router model), router balance, credentials, org-policy and
    prompt-length failures.

    Args:
        spans: the session's parsed spans, in transcript order.

    Returns:
        A stable snake_case reason slug (``model_unavailable``,
        ``router_balance_depleted``, ``credentials_missing``,
        ``org_policy_block``, ``prompt_too_long``, ``not_logged_in``,
        ``rate_limited``, ``api_error``), or ``"unknown"`` when the spans
        carry no recognizable error text. Never ``None``: by the caller's
        guard the session IS a failed start, so an unmatched text is still
        a failed start — just one we cannot name yet.
    """
    texts: list[str] = []
    for span in spans:
        out = span.attributes.get("output.value")
        if isinstance(out, str) and out.strip():
            texts.append(out)
    return failed_start_reason_from_text("\n".join(texts))


def failed_start_reason_from_text(text: str) -> str:
    """Classify a failed start straight from error text.

    The transcript-level entry point, for callers that scan
    raw ``.jsonl`` files without building spans. Same pattern table and same
    contract as :func:`failed_start_reason`: always returns a slug, with
    ``"unknown"`` for unrecognized (or absent) text.
    """
    if text and text.strip():
        for reason, pattern in _FAILED_START_PATTERNS:
            if pattern.search(text):
                return reason
    return "unknown"


@dataclass
class DiscoveredSession:
    """Lightweight session info — what a source returns from discovery.

    Carries just enough to decide whether to fetch the full transcript
    (size estimate, time bounds). The actual spans live behind a second
    call (`get_spans_by_session(session_id)`).
    """

    session_id: str
    span_count: int
    last_span_time: datetime
    start_time: datetime
    end_time: datetime


@dataclass
class SpanData:
    """One span from a parsed transcript.

    The unit every parser emits and every analyzer consumes. Shape is
    loosely inspired by OpenTelemetry's Span (parent_id, span_kind,
    status_code, attributes) while
    Claude Code / Codex / Copilot transcripts get synthesized into that
    shape by their respective parsers.

    `attributes` is the bag where every parser stashes source-specific
    fields. The convenience properties below name the ones we read
    everywhere so call sites don't sprinkle string keys.
    """

    span_id: str
    trace_id: str
    parent_id: str | None
    name: str
    span_kind: str  # AGENT | TOOL | LLM | CHAIN | ... (OpenInference-style taxonomy)
    status_code: str  # OK | ERROR | UNSET
    start_time: datetime
    end_time: datetime
    attributes: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end_time - self.start_time).total_seconds() * 1000

    @property
    def tool_name(self) -> str | None:
        return self.attributes.get("tool.name") or self.attributes.get("name")

    @property
    def input_value(self) -> str | None:
        return self.attributes.get("input.value")

    @property
    def output_value(self) -> str | None:
        return self.attributes.get("output.value")

    @property
    def is_success(self) -> bool:
        return self.status_code == "OK"

    @property
    def user_id(self) -> str | None:
        return self.attributes.get("user.id")

    @property
    def project_name(self) -> str | None:
        # Both flat ("project.name") and nested ({"project": {"name": ...}})
        # shapes: our synthesized spans emit flat, nested is also accepted.
        if "project.name" in self.attributes:
            return self.attributes["project.name"]
        project = self.attributes.get("project")
        if isinstance(project, dict):
            return project.get("name")
        return None

    @property
    def model_name(self) -> str | None:
        return self.attributes.get("llm.model_name")

    @property
    def total_tokens(self) -> int:
        return int(self.attributes.get("llm.token_count.total", 0))

    @property
    def prompt_tokens(self) -> int:
        return int(self.attributes.get("llm.token_count.prompt", 0))

    @property
    def completion_tokens(self) -> int:
        return int(self.attributes.get("llm.token_count.completion", 0))


@dataclass
class TraceData:
    """One trace — typically one agent session or one conversation turn.

    `root_span` is the synthesized `Session` span every parser emits as
    `span_id = "<trace_id>:root"`. Turn spans and tool spans hang off it.
    """

    trace_id: str
    start_time: datetime
    end_time: datetime
    spans: list[SpanData] = field(default_factory=list)

    @property
    def root_span(self) -> SpanData | None:
        for span in self.spans:
            if span.parent_id is None:
                return span
        return None

    @property
    def tool_spans(self) -> list[SpanData]:
        return [s for s in self.spans if s.span_kind == "TOOL"]


@dataclass
class SessionData:
    """A session container — one or more traces grouped by session id.

    Most sources emit one trace per session, but a session can span
    multiple traces to a single session (sub-agents, retries). Tasks
    and Themes are computed over the union of all spans across all
    traces in the SessionData.
    """

    session_id: str
    traces: list[TraceData]
    start_time: datetime
    end_time: datetime


__all__ = [
    "DiscoveredSession",
    "SessionData",
    "SpanData",
    "TraceData",
    "failed_start_reason",
    "failed_start_reason_from_text",
    "model_id_or_none",
]
