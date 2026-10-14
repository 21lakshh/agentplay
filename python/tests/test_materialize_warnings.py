"""A broken transcript database is reported once per run, not silently and not per session."""

from __future__ import annotations

import logging

import pytest

from agentplay import materialize
from agentplay.materialize import AgentKind, HomeSpec


@pytest.fixture(autouse=True)
def _fresh_warnings(monkeypatch):
    monkeypatch.setattr(materialize, "_warned", set())


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_unreadable_opencode_db_warns_once_without_the_full_path(tmp_path, caplog):
    home = tmp_path / "opencode"
    home.mkdir()
    (home / "opencode.db").write_bytes(b"this is not a sqlite database" * 20)
    spec = HomeSpec(AgentKind.OPENCODE, home)
    caplog.set_level(logging.DEBUG, logger="agentplay.materialize")

    for _ in range(2):  # a rescan in the same process stays quiet
        assert list(materialize.find_opencode_candidates(spec, cache_root=tmp_path / "c")) == []

    (message,) = _warnings(caplog)
    assert "opencode" in message and "opencode.db" in message
    assert str(tmp_path) not in message


def test_unreadable_cursor_store_db_warns_once(tmp_path, caplog):
    home = tmp_path / ".cursor"
    store = home / "chats" / "h" / "agent-1" / "store.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"this is not a sqlite database" * 20)
    spec = HomeSpec(AgentKind.CURSOR, home)
    caplog.set_level(logging.DEBUG, logger="agentplay.materialize")

    for _ in range(2):
        list(materialize.find_cursor_store_candidates(spec, cache_root=tmp_path / "c"))

    (message,) = _warnings(caplog)
    assert "cursor_store" in message and "store.db" in message
    assert str(tmp_path) not in message
