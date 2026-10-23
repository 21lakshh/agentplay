"""Writing fixes into a repo: the only place agentplay writes outside ``~/.agentplay``.

Rules, all enforced here rather than by callers:

* Only a repo the person granted (``grants.json``), at the root recorded for
  it, and only when that root still holds ``.git``.
* Only four files: ``CLAUDE.md``, ``CLAUDE.local.md``, ``.claude/settings.json``
  and ``.claude/settings.local.json``, resolved inside the root (a symlink
  that points elsewhere is refused).
* Instructions go between ``<!-- agentplay:start -->`` and ``<!-- agentplay:end -->``
  markers; text outside them is never touched. Settings are merged, never
  overwritten, and a file that is not valid JSON is left alone.
* Refused when the file changed since the preview (``expect_hash``).
* Every write is journalled in ``applied.json`` so it can be undone.

Fix text is built only from command verbs, repo-relative paths and counts
(``health.py``), so a fix cannot carry redacted content into a repo.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .store import _atomic_write

ALLOWED_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".claude/settings.json", ".claude/settings.local.json")
START = "<!-- agentplay:start -->"
END = "<!-- agentplay:end -->"
HEADING = "## Notes from agentplay"


class FixError(Exception):
    """A write that was refused; ``code`` is what the API and CLI report."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ locking

_held = threading.local()


@contextmanager
def write_lock(base: Path) -> Iterator[None]:
    """Serialize writers (server threads, the CLI and the SessionStart hook) on ``base/write.lock``.

    Re-entrant within a thread, so ``autotune.run`` can hold it across the
    ``apply`` calls it makes.
    """
    depth = getattr(_held, "depth", 0)
    if depth:
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    base.mkdir(parents=True, exist_ok=True)
    with open(base / "write.lock", "a+b") as fh:
        _lock_file(fh, True)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            _lock_file(fh, False)


def _lock_file(fh: Any, on: bool) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK if on else msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX if on else fcntl.LOCK_UN)


# ------------------------------------------------------------------ local files


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, indent=1).encode("utf-8"))


def known_roots(base: Path) -> dict[str, str]:
    """Repo name -> the checkout most recently seen for it (``repos.json``, written by builds)."""
    raw = _read_json(base / "repos.json", {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def record_roots(base: Path, roots: dict[str, str]) -> None:
    if not roots:
        return
    cur = known_roots(base)
    if all(cur.get(k) == v for k, v in roots.items()):
        return
    cur.update(roots)
    _write_json(base / "repos.json", cur)


def grants(base: Path) -> dict[str, str]:
    raw = _read_json(base / "grants.json", {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def grant(base: Path, repo: str, root: str | None = None) -> str:
    """Allow writes to ``repo``. Uses the root builds recorded unless ``root`` is given."""
    path = root or known_roots(base).get(repo)
    if not path:
        raise FixError("no_root", f"agentplay has not seen a checkout of {repo} on this machine.")
    resolved = Path(path).expanduser().resolve()
    if not (resolved / ".git").exists():
        raise FixError("not_a_repo", f"{resolved} is not a git checkout.")
    with write_lock(base):
        g = grants(base)
        g[repo] = str(resolved)
        _write_json(base / "grants.json", g)
    return str(resolved)


def revoke(base: Path, repo: str) -> None:
    with write_lock(base):
        g = grants(base)
        if g.pop(repo, None) is not None:
            _write_json(base / "grants.json", g)


def journal(base: Path) -> list[dict[str, Any]]:
    raw = _read_json(base / "applied.json", [])
    return raw if isinstance(raw, list) else []


def _save_journal(base: Path, entries: list[dict[str, Any]]) -> None:
    _write_json(base / "applied.json", entries)


def active(base: Path) -> list[dict[str, Any]]:
    return [e for e in journal(base) if not e.get("undoneAt")]


# ------------------------------------------------------------------ paths


def _target(base: Path, repo: str, rel: str) -> Path:
    if rel not in ALLOWED_FILES:
        raise FixError("file_not_allowed", f"agentplay only writes {', '.join(ALLOWED_FILES)}.")
    root_s = grants(base).get(repo)
    if not root_s:
        raise FixError("not_granted", f"Allow writes to {repo} first.")
    root = Path(root_s).resolve()
    if not (root / ".git").exists():
        raise FixError("not_a_repo", f"{root} is no longer a git checkout.")
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        raise FixError("outside_repo", f"{rel} points outside {root}.")
    return target


def file_hash(text: str | None) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def current(base: Path, repo: str, rel: str) -> str | None:
    try:
        return _target(base, repo, rel).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


# ------------------------------------------------------------------ edits


def _marker(fix_id: str) -> str:
    return f"<!-- agentplay:{fix_id} -->"


def add_lines(text: str | None, fix_id: str, lines: list[str]) -> str:
    """``text`` with ``lines`` added inside the managed block (created at the end if absent)."""
    text = text or ""
    tagged = [f"- {line} {_marker(fix_id)}" for line in lines]
    if START in text and END in text and text.index(START) < text.index(END):
        head, rest = text.split(START, 1)
        block, tail = rest.split(END, 1)
        kept = [ln for ln in block.strip("\n").split("\n") if ln.strip() and _marker(fix_id) not in ln]
        return f"{head}{START}\n" + "\n".join(kept + tagged) + f"\n{END}{tail}"
    sep = "" if not text or text.endswith("\n\n") else "\n" if text.endswith("\n") else "\n\n"
    return f"{text}{sep}{HEADING}\n\n{START}\n" + "\n".join(tagged) + f"\n{END}\n"


def remove_lines(text: str, fix_id: str) -> str:
    return "\n".join(ln for ln in text.split("\n") if _marker(fix_id) not in ln)


def _settings(text: str | None, rel: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise FixError("bad_json", f"{rel} is not valid JSON; fix it by hand first.") from exc
    if not isinstance(value, dict):
        raise FixError("bad_json", f"{rel} is not a JSON object.")
    return value


def _apply_text(action: dict[str, Any], text: str | None, fix_id: str, rel: str) -> str:
    kind = action.get("kind")
    if kind == "claude_md":
        lines = [str(x) for x in action.get("lines") or [] if str(x).strip()]
        if not lines:
            raise FixError("empty_fix", "This fix has nothing to write.")
        return add_lines(text, fix_id, lines)
    data = _settings(text, rel)
    if kind == "settings_allow":
        allow = data.setdefault("permissions", {}).setdefault("allow", [])
        entry = str(action.get("entry") or "")
        if entry and entry not in allow:
            allow.append(entry)
    elif kind == "stop_hook":
        stop = data.setdefault("hooks", {}).setdefault("Stop", [])
        cmd = str(action.get("command") or "")
        if not any(h.get("command") == cmd for g in stop for h in g.get("hooks", [])):
            stop.append({"hooks": [{"type": "command", "command": cmd}]})
    else:
        raise FixError("unknown_action", "This fix cannot be written to a file.")
    return json.dumps(data, indent=2) + "\n"


def drop_hook(groups: list[Any], command: Any) -> list[Any]:
    """``groups`` without the hook that runs ``command``; other hooks in a group stay, empty groups go."""
    out = []
    for g in groups:
        if not isinstance(g, dict):
            out.append(g)
            continue
        hooks = [h for h in g.get("hooks", []) if not (isinstance(h, dict) and h.get("command") == command)]
        if len(hooks) == len(g.get("hooks", [])):
            out.append(g)
        elif hooks:
            out.append({**g, "hooks": hooks})
    return out


def _undo_text(entry: dict[str, Any], text: str) -> str:
    kind = entry["action"].get("kind")
    if kind == "claude_md":
        return remove_lines(text, entry["fixId"])
    data = _settings(text, entry["file"])
    if kind == "settings_allow":
        allow = (data.get("permissions") or {}).get("allow") or []
        if entry["action"].get("entry") in allow:
            allow.remove(entry["action"]["entry"])
    elif kind == "stop_hook":
        stop = (data.get("hooks") or {}).get("Stop")
        if stop:
            data["hooks"]["Stop"] = drop_hook(stop, entry["action"].get("command"))
            if not data["hooks"]["Stop"]:
                del data["hooks"]["Stop"]
    return json.dumps(data, indent=2) + "\n"


def preview(base: Path, repo: str, fix: dict[str, Any], action: dict[str, Any], rel: str | None = None) -> dict[str, Any]:
    """The file as it is and as it would be, without writing."""
    rel = rel or str(action.get("file"))
    before = current(base, repo, rel)
    after = _apply_text(action, before, fix["id"], rel)
    return {"file": rel, "hash": file_hash(before), "before": before or "", "after": after}


def apply(
    base: Path,
    fix: dict[str, Any],
    action: dict[str, Any],
    *,
    rel: str | None = None,
    expect_hash: str | None = None,
    source: str = "button",
    baseline: Any = None,
) -> dict[str, Any]:
    """Write one action of ``fix`` into its repo and journal it. Returns the journal entry."""
    repo = str(fix.get("repo") or "")
    if not repo:
        raise FixError("no_repo", "This fix names an integration, not a repo; there is nothing to write.")
    rel = rel or str(action.get("file"))
    if action.get("kind") == "claude_md" and rel not in ("CLAUDE.md", "CLAUDE.local.md"):
        raise FixError("file_not_allowed", "Instructions go in CLAUDE.md or CLAUDE.local.md.")
    if action.get("kind") in ("settings_allow", "stop_hook") and not rel.startswith(".claude/"):
        raise FixError("file_not_allowed", "Settings go in .claude/settings.json or .claude/settings.local.json.")
    with write_lock(base):
        return _apply_locked(base, fix, action, repo, rel, expect_hash, source, baseline)


def _apply_locked(
    base: Path, fix: dict[str, Any], action: dict[str, Any], repo: str, rel: str,
    expect_hash: str | None, source: str, baseline: Any,
) -> dict[str, Any]:
    entries = journal(base)
    if any(e.get("fixId") == fix["id"] and e.get("repo") == repo and e.get("file") == rel
           and not e.get("undoneAt") for e in entries):
        raise FixError("already_applied", f"This fix is already in {rel}. Undo it first to write it again.")
    target = _target(base, repo, rel)
    before = target.read_text(encoding="utf-8") if target.exists() else None
    if expect_hash is not None and expect_hash != file_hash(before):
        raise FixError("changed", f"{rel} changed since the preview. Look again before applying.")
    after = _apply_text(action, before, fix["id"], rel)
    if after == before or (action.get("kind") != "claude_md" and _settings(before, rel) == _settings(after, rel)):
        # Already there (the person added it themselves): nothing to write, so nothing to undo later.
        raise FixError("already_present", f"{rel} already has this.")
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, after.encode("utf-8"))
    entry = {
        "id": uuid.uuid4().hex[:10],
        "fixId": fix["id"],
        "rule": fix.get("rule"),
        "title": fix.get("title"),
        "repo": repo,
        "file": rel,
        "action": action,
        "source": source,
        "metric": fix.get("metric"),
        "baseline": baseline,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    entries.append(entry)
    try:
        _save_journal(base, entries)
    except BaseException:
        # Never leave a repo change that cannot be undone.
        if before is None:
            target.unlink(missing_ok=True)
        else:
            _atomic_write(target, before.encode("utf-8"))
        raise
    return entry


def undo(base: Path, entry_id: str, *, reason: str = "undone") -> dict[str, Any]:
    with write_lock(base):
        return _undo_locked(base, entry_id, reason)


def _undo_locked(base: Path, entry_id: str, reason: str) -> dict[str, Any]:
    entries = journal(base)
    entry = next((e for e in entries if e.get("id") == entry_id and not e.get("undoneAt")), None)
    if entry is None:
        raise FixError("not_found", "Nothing to undo with that id.")
    target = _target(base, entry["repo"], entry["file"])
    if target.exists():
        text = target.read_text(encoding="utf-8")
        _atomic_write(target, _undo_text(entry, text).encode("utf-8"))
    entry["undoneAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["undoReason"] = reason
    _save_journal(base, entries)
    return entry


def status(base: Path) -> dict[str, Any]:
    """Repos agentplay can write to, and what it has written."""
    roots, g = known_roots(base), grants(base)
    names = sorted(set(roots) | set(g))
    return {
        "repos": [{"name": n, "root": g.get(n) or roots.get(n), "granted": n in g} for n in names],
        "applied": active(base),
    }


def home() -> Path:
    from .store import root

    return Path(os.environ.get("AGENTPLAY_HOME") or root())
