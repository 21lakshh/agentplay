"""Reading pasted images out of a Claude Code transcript: validation, caching, staleness."""

from __future__ import annotations

import base64
import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from agentplay import prompt_images as pi


def _png(rgb: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00" + bytes(rgb))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
WEBP = b"RIFF\x1a\x00\x00\x00WEBPVP8L\x0d\x00\x00\x00/\x00\x00\x00\x10\x07\x10\x11\x11\x88\x88\xfe\x07\x00"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _block(data: bytes | str, media_type: str = "image/png", source_type: str = "base64") -> dict[str, Any]:
    return {"type": "image", "source": {"type": source_type, "media_type": media_type,
                                        "data": data if isinstance(data, str) else _b64(data)}}


def _prompt(n: int, *blocks: dict[str, Any], text: str = "look", **extra: Any) -> str:
    rec = {"type": "user", "timestamp": f"2026-09-10T01:00:{n:02d}Z", "sessionId": "s",
           "message": {"role": "user", "content": [{"type": "text", "text": text}, *blocks]}, **extra}
    return json.dumps(rec) + "\n"


def _write(tmp_path: Path, *lines: str) -> str:
    f = tmp_path / "t.jsonl"
    f.write_text("".join(lines))
    return str(f)


@pytest.mark.parametrize(
    ("data", "want"),
    [
        (_png(), "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (b"GIF87a" + b"\x00" * 8, "image/gif"),
        (WEBP, "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", None),
        (b"RIFF\x00\x00\x00", None),
        (b"\x89PNG\r\n", None),
        (b"<svg xmlns=", None),
        (b"", None),
    ],
)
def test_sniff(data, want):
    assert pi._sniff(data) == want


def test_list_and_read_round_trip(tmp_path):
    a, b = _png((1, 2, 3)), _png((4, 5, 6))
    path = _write(tmp_path, _prompt(0, _block(a), _block(JPEG, "image/jpeg"), imagePasteIds=[7, 8]),
                  _prompt(1, text="no images", **{}), _prompt(2, _block(b), imagePasteIds=[9]))
    assert pi.list_images(path) == [
        {"turn": 1, "images": [{"i": 0, "pasteId": 7, "mediaType": "image/png", "available": True},
                               {"i": 1, "pasteId": 8, "mediaType": "image/jpeg", "available": True}]},
        {"turn": 3, "images": [{"i": 0, "pasteId": 9, "mediaType": "image/png", "available": True}]},
    ]
    assert pi.read_image(path, 1, 0) == ("image/png", a)
    assert pi.read_image(path, 1, 1) == ("image/jpeg", JPEG)
    assert pi.read_image(path, 3, 0) == ("image/png", b)


@pytest.mark.parametrize(
    ("ids", "want"),
    [([1], [None, None]), (["1", "2"], [None, None]), ([True, False], [None, None]), (None, [None, None]),
     ([4, 5], [4, 5])],
)
def test_paste_ids_only_when_they_pair_up(tmp_path, ids, want):
    extra = {} if ids is None else {"imagePasteIds": ids}
    path = _write(tmp_path, _prompt(0, _block(_png()), _block(_png()), **extra))
    (turn,) = pi.list_images(path)
    assert [im["pasteId"] for im in turn["images"]] == want


@pytest.mark.parametrize(
    "block",
    [
        _block(JPEG, "image/png"),  # declared png, bytes are jpeg
        _block("!!!not base64!!!"),
        _block(_png(), "image/svg+xml"),
        _block(_png(), "text/html"),
    ],
)
def test_invalid_images_are_listed_unavailable_and_refused_with_422(tmp_path, block):
    path = _write(tmp_path, _prompt(0, block))
    (turn,) = pi.list_images(path)
    assert turn["images"][0]["available"] is False
    with pytest.raises(pi.Unavailable) as err:
        pi.read_image(path, 1, 0)
    assert (err.value.status, err.value.code) == (422, "invalid_image")
    assert path not in err.value.message


def test_invalid_base64_is_refused_even_when_the_prefix_looks_right(tmp_path):
    data = _b64(_png()) + "!!!!"
    path = _write(tmp_path, _prompt(0, _block(data)))
    assert pi.list_images(path)[0]["images"][0]["available"] is True  # only a prefix is sniffed
    with pytest.raises(pi.Unavailable) as err:
        pi.read_image(path, 1, 0)
    assert err.value.status == 422


def test_non_base64_source_is_unavailable_and_404(tmp_path):
    path = _write(tmp_path, _prompt(0, {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}))
    assert pi.list_images(path) == [
        {"turn": 1, "images": [{"i": 0, "pasteId": None, "mediaType": None, "available": False}]}
    ]
    with pytest.raises(pi.Unavailable) as err:
        pi.read_image(path, 1, 0)
    assert (err.value.status, err.value.code) == (404, "not_found")


def test_oversize_is_refused_before_decoding(tmp_path, monkeypatch):
    path = _write(tmp_path, _prompt(0, _block(_png())))
    pi.list_images(path)  # scan first: it sniffs a prefix with b64decode, which is fine
    monkeypatch.setattr(pi, "MAX_IMAGE_BYTES", 8)

    def boom(*_a: Any, **_k: Any) -> bytes:
        raise AssertionError("decoded an oversize image")

    monkeypatch.setattr(pi.base64, "b64decode", boom)
    with pytest.raises(pi.Unavailable) as err:
        pi.read_image(path, 1, 0)
    assert (err.value.status, err.value.code) == (422, "invalid_image")


@pytest.mark.parametrize(("turn", "index"), [(0, 0), (2, 0), (1, 1), (1, 99)])
def test_unknown_turn_or_index_is_404(tmp_path, turn, index):
    path = _write(tmp_path, _prompt(0, _block(_png())))
    with pytest.raises(pi.Unavailable) as err:
        pi.read_image(path, turn, index)
    assert err.value.status == 404


def test_missing_transcript_is_410(tmp_path):
    path = str(tmp_path / "gone.jsonl")
    for call in (lambda: pi.list_images(path), lambda: pi.read_image(path, 1, 0)):
        with pytest.raises(pi.Unavailable) as err:
            call()
        assert (err.value.status, err.value.code) == (410, "gone")


def test_a_transcript_that_grows_is_rescanned(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(_prompt(0, _block(_png())))
    assert [t["turn"] for t in pi.list_images(str(f))] == [1]
    misses = pi._scan.cache_info().misses
    pi.list_images(str(f))
    assert pi._scan.cache_info().misses == misses  # unchanged file: served from cache
    with f.open("a") as fh:
        fh.write(_prompt(1, _block(JPEG, "image/jpeg")))
    assert [t["turn"] for t in pi.list_images(str(f))] == [1, 2]
    assert pi.read_image(str(f), 2, 0) == ("image/jpeg", JPEG)


def test_a_stale_cached_line_is_rechecked_not_trusted(tmp_path):
    """The file changes under an unchanged (mtime, size) key: the read re-parses and refuses."""
    f = tmp_path / "t.jsonl"
    f.write_bytes(_prompt(0, _block(_png())).encode())
    st = f.stat()
    assert pi.list_images(str(f))[0]["turn"] == 1
    replacement = json.dumps({"type": "user", "timestamp": "2026-09-10T01:00:00Z",
                              "message": {"role": "user", "content": [{"type": "text", "text": "x"}]}})
    f.write_bytes((replacement.ljust(st.st_size - 1) + "\n").encode())
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert f.stat().st_size == st.st_size
    with pytest.raises(pi.Unavailable) as err:
        pi.read_image(str(f), 1, 0)
    assert err.value.status == 404


def test_only_user_turns_count(tmp_path):
    """Meta, interruption and tool-result records take no turn number, as in the builder."""
    meta = {"type": "user", "isMeta": True, "timestamp": "2026-09-10T01:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "[Image: source: /tmp/2.png]"}]}}
    interrupted = {"type": "user", "timestamp": "2026-09-10T01:00:02Z",
                   "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}}
    path = _write(tmp_path, _prompt(0, _block(_png())), json.dumps(meta) + "\n", "not json\n",
                  json.dumps(interrupted) + "\n", _prompt(3, _block(JPEG, "image/jpeg")))
    assert [t["turn"] for t in pi.list_images(path)] == [1, 2]


@pytest.mark.parametrize("media_type", [["image/png"], {"t": "image/png"}, None, 7])
def test_a_non_string_media_type_is_unavailable_not_a_crash(tmp_path, media_type):
    block = _block(_png())
    block["source"]["media_type"] = media_type
    path = _write(tmp_path, _prompt(0, block))
    assert pi.list_images(path)[0]["images"][0]["available"] is False
    with pytest.raises(pi.Unavailable) as err:
        pi.read_image(path, 1, 0)
    assert (err.value.status, err.value.code) == (422, "invalid_image")


def test_an_image_exactly_at_the_size_cap_is_served(tmp_path, monkeypatch):
    png = _png() + b"\0"  # trailing byte: the base64 now ends in padding
    assert base64.b64encode(png).endswith(b"=")  # padding must not count toward the size
    path = _write(tmp_path, _prompt(0, _block(png)))
    monkeypatch.setattr(pi, "MAX_IMAGE_BYTES", len(png))
    assert pi.read_image(path, 1, 0) == ("image/png", png)


def test_hook_detail_record_replaced_by_non_object_json_is_not_found(tmp_path):
    """The line changes under an unchanged (mtime, size) key: a JSON `null` is a miss, not a 500."""
    from agentplay import hook_detail

    rec = {"type": "attachment", "uuid": "u-1", "timestamp": "2026-09-10T01:00:00Z",
           "attachment": {"type": "hook_blocking_error", "hookName": "Stop", "hookEvent": "Stop",
                          "blockingError": {"blockingError": "msg", "command": "cmd"}}}
    f = tmp_path / "t.jsonl"
    f.write_bytes((json.dumps(rec) + "\n").encode())
    st = f.stat()
    assert hook_detail.read_hook_detail(str(f), "u-1") == ("msg", "cmd")
    f.write_bytes(("null".ljust(st.st_size - 1) + "\n").encode())
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert f.stat().st_size == st.st_size
    with pytest.raises(pi.Unavailable) as err:
        hook_detail.read_hook_detail(str(f), "u-1")
    assert err.value.status == 404
