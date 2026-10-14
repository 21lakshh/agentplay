"""Separator-agnostic handling of transcript paths.

Every path here arrives as a *string* recorded on someone else's machine, and on
Windows the two producers disagree:

* ``git rev-parse --show-toplevel`` — which the CLI stamps into the manifest's
  ``repo_roots`` — always emits forward slashes, even on Windows:
  ``C:/Users/Ada/repo``.
* tool-call file paths inside a transcript are whatever the agent saw, which on
  Windows is native and backslashed: ``C:\\Users\\Ada\\repo\\src\\main.py``.

The callers compare with ``"/"`` throughout (``startswith(root + "/")``,
``rstrip("/").rsplit("/", 1)``). Against a Windows pair those comparisons would
never match: file ops would stay unresolved, sessions would not be attributed to
a repo, and project names would be the whole path instead of its last segment.

Normalizing at the boundary keeps that POSIX-shaped code correct without
teaching every call site about backslashes. Deliberately string-only: these are
remote paths from another OS, so `pathlib` — which applies the *local*
platform's rules — would be wrong here.
"""

from __future__ import annotations

import re

# A Windows absolute path, in the two forms that exist: a drive letter
# (``C:/``, ``c:\``) and a UNC share (``\\server\share``, ``//server/share``).
# Both are case-insensitive on Windows. Matched against the already-normalized
# (forward-slash) form, so only ``/`` needs handling on the UNC branch. The
# drive letter also matches bare (``C:`` end-of-string): `to_posix` trims the
# trailing slash off a drive-root path, and requiring the slash here left a
# repo whose git toplevel is the drive root un-folded — and therefore
# unattributed against its case-folded file keys.
_WINDOWS_ABSOLUTE = re.compile(r"^([A-Za-z]:(/|$)|//[^/])")


def to_posix(path: str) -> str:
    """Rewrite backslash separators as forward slashes, dropping trailing ones.

    Args:
        path: A path string produced on any platform.

    Returns:
        The path with ``/`` separators and no trailing separator; a bare root
        keeps its slash (``"/"`` stays ``"/"``).

    Example:
        >>> to_posix("C:\\\\Users\\\\Ada\\\\repo\\\\")
        'C:/Users/Ada/repo'
        >>> to_posix("/home/ada/repo")
        '/home/ada/repo'
    """
    if not path:
        return path
    normalized = path.replace("\\", "/")
    stripped = normalized.rstrip("/")
    return stripped or normalized[:1]


def basename(path: str) -> str:
    """Last segment of a path, whichever separator it uses.

    Replaces the POSIX-only ``path.rstrip("/").rsplit("/", 1)[-1]`` idiom, which
    hands back the *entire* string for a backslashed Windows path — surfacing
    ``C:\\Users\\Ada\\repo`` as a project name instead of ``repo``.

    Args:
        path: A path string produced on any platform.

    Returns:
        The final segment, or ``""`` when the input is empty.

    Example:
        >>> basename("C:\\\\Users\\\\Ada\\\\repo")
        'repo'
        >>> basename("/home/ada/repo/")
        'repo'
    """
    return to_posix(path).rsplit("/", 1)[-1] if path else ""


def match_key(path: str) -> str:
    """Canonical form for comparing two paths that may differ in style.

    Beyond separators, Windows paths are case-insensitive and the case varies by
    producer (``C:/`` from git, ``c:\\`` from a shell), so a case-sensitive compare
    can still miss. Both Windows absolute forms count: drive letters *and* UNC
    shares — ``//Server/Share/repo`` from git against
    ``\\server\\share\\repo\\src\\main.py`` from a transcript is the same
    path on a network drive, and must also compare equal.

    Case folding applies **only** to those forms: POSIX filesystems are
    case-sensitive, and folding them would conflate genuinely distinct files.

    Args:
        path: A path string produced on any platform.

    Returns:
        A string safe to compare against — or prefix-match with — the
        `match_key` of another path.

    Example:
        >>> match_key("C:\\\\Users\\\\Ada\\\\Repo") == match_key("c:/users/ada/repo")
        True
        >>> match_key("/home/Ada") == match_key("/home/ada")
        False
    """
    normalized = to_posix(path)
    return normalized.casefold() if _WINDOWS_ABSOLUTE.match(normalized) else normalized
