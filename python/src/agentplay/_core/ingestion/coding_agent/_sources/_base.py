"""TraceSource Protocol — what every coding-agent source implements.

Sources discover sessions and parse transcripts into the universal
`SpanData` shape. Anything that conforms to this Protocol can plug into
the ingestion pipeline without the rest of the code knowing where the
data came from.

# Why a Protocol and not an ABC

Sources are duck-typed across multiple parser implementations (local
Claude, local Codex, local Copilot, and so on). A `Protocol` needs no
inheritance chain and no metaclass gymnastics. `@runtime_checkable` makes
`isinstance(x, TraceSource)` work at the factory boundary.

# Identity convention — source-prefixed session IDs

Every source emits `session_id` strings prefixed by the source name
so collisions across sources are impossible:

- `local:{agent}:{file_stem}`

The prefix is also how the `source` value on a session is derived.
Sources MUST emit the same prefix consistently: it must match
`source.source_name`.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Protocol, runtime_checkable

from agentplay._core.ingestion.coding_agent.metrics import session_tool_distribution
from agentplay._core.ingestion.coding_agent.spans import DiscoveredSession, SpanData

# Hard backstop only: a transcript larger than this is rejected rather than
# parsed. It exists to catch pathological multi-GB transcripts that would
# exhaust memory, and is set high so a normal-but-heavy session never trips it.
MAX_SESSION_BYTES = 1024 * 1024 * 1024  # 1 GB


class OversizedSessionError(Exception):
    """A session's raw transcript exceeds ``MAX_SESSION_BYTES``.

    Raised by the local source while it reads a session (and any subagent
    transcripts merged into it) when the combined size passes the cap. The
    transcript is not parsed.
    """


def build_sidecar_signals(spans: list[SpanData]) -> dict:
    """Session-payload signal blobs derived from a session's spans.

    Pulls the parser-stashed ``harness.session_signals`` (slash commands, skills used,
    captured skill definitions, interrupts) off the root span and adds the
    per-tool distribution computed from the spans. These are opaque count
    blobs kept in the session payload rather than on per-session fields.

    ``skill_definitions`` must stay in the key list below: omitting it
    silently drops captured SKILL.md bodies for every source relying on
    this shared helper.
    """
    out: dict = {}
    root = next((s for s in spans if s.parent_id is None), None)
    if root is not None:
        raw = root.attributes.get("harness.session_signals")
        if raw:
            try:
                sig = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                sig = None
            if isinstance(sig, dict):
                for key in (
                    "slash_commands",
                    "slash_invocations",
                    "skills_used",
                    "skill_definitions",
                    "interrupts",
                ):
                    if sig.get(key):
                        out[key] = sig[key]
    tool_dist = session_tool_distribution(spans)
    if tool_dist:
        out["tool_distribution"] = tool_dist
    return out


@runtime_checkable
class TraceSource(Protocol):
    """The contract every coding-agent source adapter satisfies.

    The built-in implementation lives under `local/`.
    """

    source_name: str
    """Currently 'local'. Used as the prefix for
    `session_id` values this source emits and as the `source` value on
    the resulting session."""

    def capabilities(self) -> frozenset[str]:
        """What this source can produce. Used by extractors to decide
        whether they have enough signal — e.g. token counts may be
        absent for some sources, in which case `total_cost_usd`
        is left null on the Session."""
        ...

    def discover_sessions(
        self,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> list[DiscoveredSession]:
        """Walk the source and list every session in `[start_time, end_time]`.

        Returns lightweight `DiscoveredSession` rows — span count + time
        bounds, enough for the pipeline to decide what to fetch. The
        actual transcript content arrives via `get_spans_by_session`.
        """
        ...

    def get_spans_by_session(self, session_id: str) -> list[SpanData]:
        """Parse the session's transcript into a span list.

        Returned spans MUST include a root `Session` span (with
        `parent_id is None`) carrying session-level attributes
        (`user.id`, `llm.model_name`, `session.id`, optional
        `project.name`, plus any source-specific harness signals
        under `harness.*` keys).
        """
        ...


__all__ = ["TraceSource"]
