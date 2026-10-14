"""The publish-time leak scanner flags leak shapes and passes placeholders."""

import importlib.util
from pathlib import Path

import pytest

_script = Path(__file__).resolve().parents[2] / "scripts" / "check_clean.py"
if not _script.is_file():
    pytest.skip("scripts/ is only in a repo checkout, not the sdist", allow_module_level=True)
_spec = importlib.util.spec_from_file_location("check_clean", _script)
check_clean = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_clean)  # type: ignore[union-attr]


def test_flags_leak_shapes() -> None:
    assert "ticket id" in check_clean.scan_line("fixes ZZ" + "-123")
    assert "cross-repo PR ref" in check_clean.scan_line("see widgets" + "#1234")
    assert "personal home path" in check_clean.scan_line("/Users" + "/bob/project")
    assert "personal home path" in check_clean.scan_line("~/.claude/projects/-Users" + "-bob-app")
    assert "personal home path" in check_clean.scan_line("bobs" + "-MacBook-Pro.local")
    assert "real-looking email" in check_clean.scan_line("mail bob" + "@corp.io")


def test_placeholders_pass() -> None:
    for line in (
        "UTF-8 and SHA-256", "acme/app#1234", "ACME-42", "/Users/you/x", "a@example.com",
        "-Users-ada-work-app", "adas-MacBook-Pro.local",
    ):
        assert check_clean.scan_line(line) == [], line
