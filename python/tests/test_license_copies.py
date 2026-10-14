"""python/ ships copies of the repo's license files; they must match the originals."""

from pathlib import Path

import pytest

PYTHON = Path(__file__).resolve().parents[1]
REPO = PYTHON.parent


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"])
def test_copy_matches_repo_root(name: str) -> None:
    if not (REPO / name).is_file():
        pytest.skip("not in a repo checkout (e.g. an unpacked sdist)")
    assert (PYTHON / name).read_bytes() == (REPO / name).read_bytes(), f"copy {name} to python/{name}"
