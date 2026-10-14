"""Claude Code transcript role detection."""

from __future__ import annotations

import json


def claude_subagent_transcript(data: bytes) -> bool:
    """Whether this transcript is a Claude Code subagent's, not a session of its own.

    Claude Code writes a subagent's turns to their own file and marks every
    conversation record ``isSidechain: true``. Newer builds front-load metadata
    records (``queue-operation``, ``last-prompt``, ``ai-title``) that carry no
    flag, so checking only the first line lets a subagent file pass as a
    standalone session.

    The test is ALL, not ANY: a real session that merely used subagents has its
    own unflagged turns. Only a transcript whose every conversation record is
    flagged is itself a subagent's.

    Args:
        data: raw transcript bytes.

    Returns:
        True only when at least one conversation record is a sidechain and none
        is not. False for an empty, unparseable, or ordinary transcript.
    """
    saw_sidechain = False
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") not in ("user", "assistant"):
            continue
        if record.get("isSidechain") is True:
            saw_sidechain = True
            continue
        return False
    return saw_sidechain
