"""Skill-name extraction and validation.

A "skill" is a Claude Code skill directory (``skills/<name>/SKILL.md``); the
``<name>`` segment is a kebab-case identifier. Two layers must agree on what a
valid skill name is:

* extraction - ``extract_session_signals`` records ``skills_used`` from
  transcript signals (``_sources/_jsonl_utils.py``);
* display - anything that lists those skills.

Keeping the rule in one place stops garbage captured at extraction (e.g. a regex
literal that quotes the skill-load phrase) from leaking into output, and keeps
the display-side filter from disagreeing with the parser about what counts as
a skill.
"""

from __future__ import annotations

import re

from agentplay._core.ingestion.coding_agent._paths import to_posix

# Skill dir names are kebab-case identifiers: first char alnum, then alnum / dot
# / underscore / hyphen, bounded length. Deliberately rejects quotes, parens,
# backticks, backslashes, and whitespace — the shapes that garbage captures
# (code / regex fragments quoting "base directory for this skill:") take.
# Directory names that hold skills rather than being one. Matched as whole
# path segments — see `skill_name_from_path`.
_SKILL_ROOT_SEGMENTS = frozenset({"skills", "skills-cursor"})
_PLUGIN_COMMANDS_SEGMENT = "commands"
_PLUGINS_SEGMENT = "plugins"

# Names that are never skills: the first segment of an absolute path
# (`/Users/…` → `Users`, `/private/var/…` → `private`), a plugin's folder names
# (`plugins`, `common`), and Claude Code's own tools (`ExitPlanMode`). Rejected
# on read as well as on write, so stored values with these names do not show
# as skills.
_NOT_SKILL_NAMES = frozenset(
    {
        # OS / home-directory path segments
        "Users",
        "home",
        "private",
        "tmp",
        "var",
        "opt",
        "Volumes",
        "Library",
        "Applications",
        "Desktop",
        "Documents",
        "Downloads",
        # plugin layout folders
        "plugins",
        "common",
        "synced",
        # Claude Code tools
        "Agent",
        "Bash",
        "Edit",
        "EnterPlanMode",
        "ExitPlanMode",
        "Glob",
        "Grep",
        "MultiEdit",
        "NotebookEdit",
        "Read",
        "Skill",
        "Task",
        "TodoWrite",
        "WebFetch",
        "WebSearch",
        "Write",
    }
)

# An optional `<plugin>:` namespace is kept so a plugin skill stays
# attributable to its plugin. 128 caps the whole name length.
_VALID_SKILL_NAME = re.compile(
    r"(?=.{1,128}\Z)[A-Za-z0-9][A-Za-z0-9._-]{0,79}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,79})?"
)


def skill_name_from_path(raw: str) -> str:
    """Reduce a skill path to its display name; best-effort, never empty.

    ``skills/dark-mode-ui/SKILL.md`` -> ``dark-mode-ui``. Strips a leading store
    prefix (``.../.claude/...`` or ``.../.cursor/...``), isolates the segment
    after the last ``skills/``, and drops any trailing ``/SKILL.md`` or
    sub-path. Does NOT validate the result — pair with
    :func:`is_valid_skill_name`.

    Both stores are stripped so a Cursor skill path resolves identically to a
    Claude one. It matters when an EARLIER path segment is itself named
    ``skills`` (``/home/skills/.cursor/skills/a11y/SKILL.md``): without the
    store strip the split below latches onto that first segment and yields
    ``.cursor`` instead of ``a11y``.

    Plugin skills: a namespaced invocation ``myplugin:deploy``
    is kept whole, so the plugin stays visible; a namespaced invocation whose
    command is itself nested (``myplugin:common/engine/myplugin-run``) keeps the
    namespace and the command's LEAF segment (``myplugin:myplugin-run``); a
    plugin command path ``plugins/cache/m/myplugin/1.0/commands/
    deploy.md`` resolves to ``deploy``; a path under ``plugins/``
    with neither a skills nor a commands segment resolves to ``"unknown"``
    rather than to ``plugins``.

    An absolute path (``/Users/…``, ``~/…``) with no skills or commands
    segment also resolves to ``"unknown"``: its first segment is a root
    folder such as ``Users`` or ``tmp``, not a skill. A relative
    ``my-skill/SKILL.md`` still resolves to ``my-skill``.

    Args:
        raw: A skill path or bare name as recorded in ``skills_used``.

    Returns:
        The skill name segment, or ``"unknown"`` when ``raw`` is empty or names
        no skill.
    """
    stripped = (raw or "").strip()
    # An absolute path (`/Users/…`, `~/…`) whose skill can't be located below
    # resolves to no name rather than to its first segment.
    is_absolute = stripped.startswith(("/", "~"))
    cleaned = stripped.strip("/")
    if not cleaned:
        return "unknown"
    # A plugin's own command can itself be nested (`myplugin:common/engine/
    # myplugin-run`): checked BEFORE the general slash
    # split below, which otherwise treats the whole string as a plain path
    # and returns just its first segment (`myplugin:common`), dropping the
    # actual command. The leaf after the last `/` is the
    # command; the namespace is kept as sent, unsplit.
    ns, sep, rest = cleaned.partition(":")
    if sep and ns and "/" not in ns and rest:
        leaf = rest.rstrip("/").rsplit("/", 1)[-1]
        if leaf:
            return f"{ns}:{leaf}"
    for store_prefix in ("/.claude/", "/.cursor/"):
        if store_prefix in cleaned:
            cleaned = cleaned.split(store_prefix, 1)[1]
            break
    # Both skills ROOTS: Cursor ships its bundled skills under
    # `~/.cursor/skills-cursor/<name>/`, which contains no `skills/` segment at
    # all. Without handling it the FIRST path segment is returned as the name —
    # `skills-cursor` for a `.cursor` path, `Users` for one whose store prefix
    # was not stripped.
    #
    # Matched as a WHOLE SEGMENT, not a substring. A
    # bare `"skills-cursor/" in cleaned` also fires on
    # `skills/skills-cursor/SKILL.md` — a skill genuinely NAMED `skills-cursor`
    # — and hands back `SKILL.md`, which `is_valid_skill_name` happily accepts,
    # so the skill list shows a skill called `SKILL.md`. It would equally fire
    # inside an unrelated segment such as `my-skills-cursor/`.
    segments = [seg for seg in cleaned.split("/") if seg]
    for i, seg in enumerate(segments):
        if seg in _SKILL_ROOT_SEGMENTS and i + 1 < len(segments):
            return segments[i + 1]
    if not segments:
        return "unknown"
    # Plugin slash commands live at `<plugin>/commands/<name>.md` and are invoked
    # through the same Skill tool, so they resolve like a skill directory.
    for i, seg in enumerate(segments[:-1]):
        if seg == _PLUGIN_COMMANDS_SEGMENT:
            name = segments[i + 1]
            return name[: -len(".md")] if name.endswith(".md") else name
    # A plugin's own root (`plugins/cache/<marketplace>/<plugin>/…`) names no skill;
    # falling through would report the folder `plugins` as one.
    if len(segments) > 1 and _PLUGINS_SEGMENT in segments:
        return "unknown"
    # A relative `my-skill/SKILL.md` names its skill first; an absolute path's
    # first segment is a root folder (`Users`, `tmp`, `private`), never a skill.
    if is_absolute and len(segments) > 1:
        return "unknown"
    return segments[0]


def is_valid_skill_name(name: str) -> bool:
    """True when ``name`` is a plausible skill identifier (kebab-case, bounded).

    Rejects the garbage shapes seen in real transcripts (backticks, quotes, parens, regex
    fragments) while accepting real skills like ``ui-ux-pro-max`` or
    ``web-design-guidelines``. Requires at least one letter, so version-like
    tokens (``2.1.159``, ``1.0.0``) that leak from transcripts are dropped —
    real skill names always have letters. Also rejects the known non-skill
    names in ``_NOT_SKILL_NAMES`` (path segments, plugin folders, tool names).
    """
    return (
        bool(name)
        and _VALID_SKILL_NAME.fullmatch(name) is not None
        and any(c.isalpha() for c in name)
        and name not in _NOT_SKILL_NAMES
    )


def normalize_skill_name(raw: str) -> str | None:
    """Extract and validate a skill name from a raw path/name.

    Args:
        raw: A skill path or bare name (e.g. ``skills/frontend-design``).

    Returns:
        The clean skill name, or ``None`` when ``raw`` doesn't resolve to a
        plausible skill (garbage captured from quoted code, regex literals, …).
        Blank/empty input resolves to the ``"unknown"`` sentinel, which is
        treated as no skill (``None``) rather than a real name.
    """
    name = skill_name_from_path(raw)
    if name == "unknown":  # sentinel for empty/unresolvable input, not a real skill
        return None
    return name if is_valid_skill_name(name) else None


def skill_name_from_skill_md_path(file_path: str) -> str | None:
    """Canonical skill name from a ``.../<name>/SKILL.md`` path, or None.

    The name is the SKILL.md file's immediate PARENT DIRECTORY, not (as
    :func:`skill_name_from_path` assumes) the segment after a literal
    ``"skills/"`` anchor. There is no single fixed skills root in the wild:
    real captured examples include ``~/.cursor/skills-cursor/<name>/``
    (no ``"skills/"`` substring at all), ``~/.codex/skills/.system/<name>/``
    (the anchor rule grabs ``".system"``), project-local
    ``<repo>/.claude/skills/<name>/`` and marketplace-plugin caches nested
    arbitrarily deep. Reuses only the shared kebab-case validation.

    This is the ONE derivation every SKILL.md-read detector (`cursor_store`
    and the others) calls, so a name can never differ between detectors for
    the same path string.

    Args:
        file_path: An absolute or relative path to a SKILL.md file. The caller
            is responsible for confirming the basename is ``SKILL.md``; this
            function does not re-check it.

    Returns:
        The parent directory name when it is a plausible skill identifier,
        else ``None`` (too short a path, or a garbage/invalid name).

    Example:
        >>> skill_name_from_skill_md_path("/Users/x/.cursor/skills-cursor/autopilot/SKILL.md")
        'autopilot'
    """
    segments = [s for s in to_posix(file_path).split("/") if s]
    if len(segments) < 2:
        return None
    name = segments[-2]
    return name if is_valid_skill_name(name) else None


def skill_summary(text: str, *, limit: int = 160) -> str:
    """One-line description of an authored skill, from its SKILL.md body.

    Prefers the frontmatter ``description:`` field; otherwise the first prose
    line (skipping YAML frontmatter, Markdown headings, and blanks). Returns ""
    when nothing usable is found. Truncated to ``limit`` chars.

    Lives here beside the name rules so every consumer gets the same
    answer. The ``limit`` default keeps the text short enough to compare
    by token overlap: an untruncated body dilutes the score.

    Args:
        text: The full SKILL.md body, or "" when no definition was captured.
        limit: Max characters of the extracted description.

    Returns:
        The description line, truncated, or "" when nothing usable is found.
    """
    if not text:
        return ""
    in_frontmatter = False
    for i, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if i == 0 and line == "---":
            in_frontmatter = True
            continue
        if in_frontmatter:
            if line == "---":
                in_frontmatter = False
                continue
            if line.lower().startswith("description:"):
                desc = line.split(":", 1)[1].strip().strip("'\"")
                if desc:
                    return desc[:limit].rstrip()
            continue
        if not line or line.startswith("#") or line.startswith("---"):
            continue
        return line[:limit].rstrip()
    return ""


# tool names that load a skill, and the span attribute carrying the
# invoked name so a consumer never has to re-parse a truncated ``input.value``.
SKILL_TOOL_NAMES = frozenset({"skill", "activate_skill"})
SKILL_CALL_NAME_ATTR = "skill.name"


def skill_call_attributes(tool_name: str, tool_input: object) -> dict[str, str]:
    """Span attributes for a skill-loading tool call: its raw invoked name.

    Args:
        tool_name: The tool_use block's ``name`` (``Skill``, ``activate_skill``, …).
        tool_input: The tool_use block's ``input`` (a dict for Claude Code).

    Returns:
        ``{"skill.name": <raw name>}`` for a Skill call that names a skill,
        else ``{}``. The name is raw (normalize with :func:`skill_call_name`).
    """
    if str(tool_name).strip().lower() not in SKILL_TOOL_NAMES or not isinstance(tool_input, dict):
        return {}
    raw = tool_input.get("skill") or tool_input.get("command")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    return {SKILL_CALL_NAME_ATTR: raw.strip()[:256]}


def skill_call_name(tool_name: str, attributes: dict | None, input_value: object = None) -> str | None:
    """Canonical skill name of a skill-loading tool call, or None.

    Prefers the ``skill.name`` span attribute, then the
    JSON ``input.value``. A leading ``/`` is dropped, and the name goes
    through :func:`normalize_skill_name`, so junk (``common``) stays None.

    Args:
        tool_name: The tool's name; only ``Skill``/``activate_skill`` qualify.
        attributes: The span's attributes (may be None).
        input_value: The span's ``input.value`` JSON string, if any.

    Returns:
        ``plugin:skill`` or ``skill``, or None when the call names no real skill.

    Example:
        >>> skill_call_name("Skill", {"skill.name": "myplugin:deploy"})
        'myplugin:deploy'
    """
    if str(tool_name).strip().lower() not in SKILL_TOOL_NAMES:
        return None
    raw = (attributes or {}).get(SKILL_CALL_NAME_ATTR)
    if not isinstance(raw, str) or not raw.strip():
        raw = None
        if isinstance(input_value, str) and input_value:
            import json

            try:
                payload = json.loads(input_value)
            except (json.JSONDecodeError, TypeError):
                payload = None
            if isinstance(payload, dict):
                cand = payload.get("skill") or payload.get("command")
                raw = cand if isinstance(cand, str) else None
    if not raw:
        return None
    return normalize_skill_name(raw.strip().lstrip("/"))
