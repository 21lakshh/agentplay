"""Images pasted into Claude Code prompts, read on demand and never persisted.

A map carries only an ``images`` count per user turn. To show the pictures
themselves the local server opens the original transcript when the viewer asks
and returns the bytes for that one image. Nothing here writes to a map, the
index, an export or disk: the scan below caches *metadata and a line number*
(never image data), and a read re-parses that single line.

Turns are numbered by :func:`agentplay.builder.user_turns`, exactly as the
builder numbers its ``user`` events and :func:`agentplay.discover.local_task_titles`
labels them, so turn ``k`` here is the task whose ``firstTurn`` is ``k`` and a
viewer can pair an image with a task without any index of its own.

Callers (the server) decide who may ask. This module trusts the path it is given.
"""

from __future__ import annotations

import base64
import binascii
import functools
import itertools
import json
import os
from typing import Any, NamedTuple

from .builder import user_turn_text, user_turns

MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 10 * 1024 * 1024
"""Largest decoded image served. Checked on the base64 length, before decoding."""

_PREFIX_CHARS = 16  # a multiple of 4: 12 decoded bytes, enough for every magic number below


class Unavailable(Exception):
    """An image that cannot be served, with the HTTP status and error code to report.

    The message is a fixed string: it never names a path or echoes transcript data.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _not_found() -> Unavailable:
    return Unavailable(404, "not_found", "We couldn't find that image.")


def _gone() -> Unavailable:
    return Unavailable(410, "gone", "The transcript for this session is no longer on this machine.")


def _invalid() -> Unavailable:
    return Unavailable(422, "invalid_image", "That image can't be shown.")


class _Image(NamedTuple):
    i: int
    paste_id: int | None
    media_type: str | None
    available: bool


class _Turn(NamedTuple):
    turn: int
    line: int  # 0-based line index in the transcript
    images: tuple[_Image, ...]


def _sniff(data: bytes) -> str | None:
    """Media type implied by the leading bytes of an image, or None if unrecognised."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_blocks(rec: dict[str, Any]) -> list[dict[str, Any]]:
    message = rec.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [x for x in content if isinstance(x, dict) and x.get("type") == "image"]


def _source(block: dict[str, Any]) -> dict[str, Any] | None:
    """The block's ``base64`` source, or None for a URL/file source or a malformed block."""
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "base64":
        return source
    return None


def _matches_media_type(source: dict[str, Any]) -> bool:
    """Whether the declared media type is allowed and the first bytes agree with it."""
    media_type, data = source.get("media_type"), source.get("data")
    if not isinstance(media_type, str) or media_type not in MEDIA_TYPES or not isinstance(data, str):
        return False
    try:
        head = base64.b64decode(data[:_PREFIX_CHARS], validate=True)
    except (binascii.Error, ValueError):
        return False
    return _sniff(head) == media_type


def _paste_ids(rec: dict[str, Any], count: int) -> list[int | None]:
    ids = rec.get("imagePasteIds")
    if (
        isinstance(ids, list)
        and len(ids) == count
        and all(isinstance(x, int) and not isinstance(x, bool) for x in ids)
    ):
        return list(ids)
    return [None] * count


def _stat(path: str) -> os.stat_result:
    try:
        return os.stat(path)
    except FileNotFoundError:
        raise _gone() from None


@functools.lru_cache(maxsize=16)
def _scan(path: str, mtime_ns: int, size: int) -> tuple[_Turn, ...]:
    """Metadata for every turn with an image block. Holds no image data.

    ``mtime_ns`` and ``size`` are only the cache key: a transcript that grows
    gets a fresh scan.
    """
    turns: list[_Turn] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        records = user_turns(fh)
    for n, (line_no, rec, _) in enumerate(records, start=1):
        blocks = _image_blocks(rec)
        if not blocks:
            continue
        ids = _paste_ids(rec, len(blocks))
        images = []
        for i, block in enumerate(blocks):
            source = _source(block)
            images.append(
                _Image(
                    i=i,
                    paste_id=ids[i],
                    media_type=source.get("media_type") if source else None,
                    available=source is not None and _matches_media_type(source),
                )
            )
        turns.append(_Turn(turn=n, line=line_no, images=tuple(images)))
    return tuple(turns)


def _scan_file(path: str) -> tuple[_Turn, ...]:
    st = _stat(path)
    try:
        return _scan(path, st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        raise _gone() from None
    except OSError:
        raise _not_found() from None


def list_images(path: str) -> list[dict[str, Any]]:
    """Per-turn image metadata for a transcript, only for turns that have images.

    Raises:
        Unavailable: 410 if the transcript is gone.
    """
    return [
        {
            "turn": t.turn,
            "images": [
                {"i": im.i, "pasteId": im.paste_id, "mediaType": im.media_type, "available": im.available}
                for im in t.images
            ],
        }
        for t in _scan_file(path)
    ]


def read_image(path: str, turn: int, index: int) -> tuple[str, bytes]:
    """Decode one pasted image from the transcript.

    Args:
        path: the transcript ``.jsonl``.
        turn: 1-based user-turn number, as in :func:`list_images`.
        index: 0-based position among that turn's image blocks.

    Returns:
        ``(media_type, bytes)``, the type being the sniffed one.

    Raises:
        Unavailable: 410 if the transcript is gone; 404 for an unknown turn or
            index or a block whose source is not inline base64; 422 for a
            disallowed media type, invalid base64, bytes that are not the declared
            type, or an image over ``MAX_IMAGE_BYTES``.
    """
    entry = next((t for t in _scan_file(path) if t.turn == turn), None)
    if entry is None or not 0 <= index < len(entry.images):
        raise _not_found()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            line = next(itertools.islice(fh, entry.line, None), "")
    except FileNotFoundError:
        raise _gone() from None
    except OSError:
        raise _not_found() from None
    try:
        rec = json.loads(line)
    except ValueError:
        raise _not_found() from None
    # The file may have changed since the scan; trust nothing the cached line says.
    if user_turn_text(rec) is None:
        raise _not_found()
    blocks = _image_blocks(rec)
    if index >= len(blocks):
        raise _not_found()
    source = _source(blocks[index])
    if source is None:
        raise _not_found()
    media_type, data = source.get("media_type"), source.get("data")
    if not isinstance(media_type, str) or media_type not in MEDIA_TYPES or not isinstance(data, str):
        raise _invalid()
    if len(data) * 3 // 4 - data[-2:].count("=") > MAX_IMAGE_BYTES:
        raise _invalid()
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise _invalid() from None
    if _sniff(raw) != media_type:
        raise _invalid()
    return media_type, raw
