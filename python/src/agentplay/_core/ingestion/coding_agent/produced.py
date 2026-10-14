"""PR creation evidence shared by raw transcript parsing and structured tool records."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
import shlex

from agentplay._core.ingestion.coding_agent.spans import SpanData

_GH_PR = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)")
_GL_MR = re.compile(
    r"https?://(gitlab[A-Za-z0-9_.-]*\.[a-z]+)/([A-Za-z0-9_./-]+)/-/merge_requests/(\d+)"
)
_SHELL_TOOLS = {"bash", "shell"}


@dataclass(frozen=True, slots=True)
class CreatedPR:
    """A successful create invocation's single output URL and observed turn."""

    provider: str
    repo_name: str
    number: int
    url: str
    source_event_id: str | None = field(default=None, compare=False)
    command_sha256: str | None = field(default=None, compare=False)
    created_in_turn: int | None = None


def _command_value(value: object) -> str:
    """Unwrap native shell args or a single literal Codex Desktop tool call."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            pass
        else:
            value = parsed if isinstance(parsed, dict) else None
    if isinstance(value, dict):
        value = value.get("command") or value.get("cmd")
    if not isinstance(value, str):
        return ""
    # Desktop wrappers can also arrive inside native shell args. Parse only
    # one exact invocation after unwrapping; never search arbitrary JavaScript.
    match = re.fullmatch(
        r"\s*(?:(?:const|let)\s+[A-Za-z_$][\w$]*\s*=\s*)?await\s+tools\.exec_command\((\{.*\})\)\s*;?\s*",
        value,
        re.DOTALL,
    )
    if not match:
        return value
    try:
        args = json.loads(match.group(1))
    except (ValueError, RecursionError):
        return ""
    command = args.get("command") or args.get("cmd")
    return command if isinstance(command, str) else ""


def command_text(span: SpanData) -> str:
    """Read the recorded command without coercing arbitrary structured input."""
    return _command_value(span.input_value)


def _ordinary_body_path(value: str) -> bool:
    """Accept literal file paths while excluding stream/device namespaces."""
    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or ".." in parts or normalized.startswith(("//./", "//?/")):
        return False
    if normalized.startswith("/") and parts[0] in {"dev", "proc", "sys"}:
        return False
    return parts[-1].split(".")[0].upper() not in {"CON", "CONIN$", "CONOUT$", "PRN", "AUX", "NUL"}


def _silent_body_file(match: re.Match[str]) -> str:
    """Mask only a literal pathlib write, never arbitrary Python stdout."""
    try:
        nodes = ast.parse(match.group(2)).body
    except (SyntaxError, ValueError, RecursionError):
        return match.group(0)
    if len(nodes) != 2:
        return match.group(0)
    imported, expression = nodes
    if not (
        isinstance(imported, ast.ImportFrom)
        and imported.level == 0
        and imported.module == "pathlib"
        and len(imported.names) == 1
        and imported.names[0].name == "Path"
        and imported.names[0].asname is None
        and isinstance(expression, ast.Expr)
        and isinstance(expression.value, ast.Call)
    ):
        return match.group(0)
    call = expression.value
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "write_text"):
        return match.group(0)
    path = call.func.value
    if not (
        isinstance(path, ast.Call)
        and isinstance(path.func, ast.Name)
        and path.func.id == "Path"
        and len(path.args) == 1
        and isinstance(path.args[0], ast.Constant)
        and isinstance(path.args[0].value, str)
        and _ordinary_body_path(path.args[0].value)
        and not path.keywords
        and len(call.args) == 1
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and all(
            k.arg == "encoding"
            and isinstance(k.value, ast.Constant)
            and k.value.value in {"utf-8", "utf8"}
            for k in call.keywords
        )
    ):
        return match.group(0)
    return ""


def _literal_shell(command: str) -> str:
    """Mask only quoted literal here-documents in known body-file idioms.

    Arbitrary command substitutions, unquoted heredocs and shell evaluation
    remain unsupported. The original command, not this analysis copy, is hashed.
    """
    command = command.replace("\\\n", "")
    command = re.sub(
        r"""\$\(gh auth token --user [A-Za-z0-9_-]+\)""",
        "credential-from-provider",
        command,
    )
    command = re.sub(
        r'''"\$\(cat\s+<<'([A-Za-z_][A-Za-z_0-9]*)'\n.*?\n\1\n\)"''',
        '"literal-body"',
        command,
        flags=re.DOTALL,
    )
    command = re.sub(
        r"(?m)^cat\s+>\s+(?P<body_file>[/A-Za-z0-9_.-]+)\s+<<'(?P<eof>[A-Za-z_][A-Za-z_0-9]*)'\n.*?\n(?P=eof)(?:\n|$)",
        lambda match: "" if _ordinary_body_path(match.group("body_file")) else match.group(0),
        command,
        flags=re.DOTALL,
    )
    command = re.sub(
        r"(?m)^python(?:3(?:\.\d+)?)? - <<'([A-Za-z_][A-Za-z_0-9]*)'\n(.*?)\n\1\n(?=\S)",
        _silent_body_file,
        command,
        flags=re.DOTALL,
    )
    return command if "$(" not in command and "`" not in command else ""


def _create_invocation(command: str) -> tuple[str, list[str], dict[str, str]] | None:
    """Accept a direct create, optionally after cd or git add/commit/push; never execute.

    A head/tail filter requires guarded pipefail; other pipelines and multiple
    output-producing commands are ambiguous. Quoted arguments may contain substitutions.
    Quoted mentions in Python, echo, --body and comments are not invocations.
    """
    try:
        lexer = shlex.shlex(
            _literal_shell(command).strip(), posix=True, punctuation_chars=";&|()<>\n"
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    pipefail = False
    if len(tokens) >= 4 and tokens[:1] == ["set"] and tokens[2] == "pipefail" and tokens[3] == "&&":
        pipefail = tokens[1] in {"-o", "-eo", "-euo", "-uo"}
        if not pipefail:
            return None
        tokens = tokens[4:]
    # Only non-PR preparatory commands may surround a single create. No echo,
    # view, branch, loop, function, OR fallback or arbitrary executable can
    # manufacture the URL subsequently used as creation evidence.
    parts = []
    current = []
    for token in tokens:
        if token in {"&&", ";"} or token.strip("\n") == "":
            if current:
                parts.append((current, token))
                current = []
        else:
            current.append(token)
    if current:
        parts.append((current, None))
    creates = []
    for part, separator in parts:
        if part[:1] == ["set"] and len(part) == 2 and re.fullmatch(r"-[eu]+", part[1]):
            continue
        if (
            len(part) == 3
            and part[0] == "set"
            and part[1] in {"-o", "-eo", "-euo", "-uo"}
            and part[2] == "pipefail"
        ):
            pipefail = True
            continue
        if part[:1] == ["cd"] and len(part) == 2:
            continue
        if len(part) == 1 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^;&|()<>]*", part[0]):
            continue
        # `git add/commit/push && gh pr create`: git prefixes remote text with
        # `remote:` and prints no PR URL of its own, so the single-URL line rule
        # below still attributes only the create's output. `&&` makes the create
        # conditional on them; no redirect, pipe or fallback may ride along.
        if (
            not creates
            and separator == "&&"
            and part[:1] == ["git"]
            and part[1:2] in (["add"], ["commit"], ["push"])
            and not any(re.fullmatch(r"[;&|()<>]+", token) for token in part)
        ):
            continue
        creates.append(part)
    if len(creates) != 1:
        return None
    tokens = creates[0]
    if tokens[:1] == ["env"]:
        tokens = tokens[1:]
    environment = {}
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0], re.DOTALL):
        key, value = tokens[0].split("=", 1)
        environment[key] = value
        tokens = tokens[1:]
    if "|" in tokens:
        if not pipefail or tokens.count("|") != 1:
            return None
        split = tokens.index("|")
        suffix = tokens[split + 1 :]
        if (
            not suffix
            or suffix[0] not in {"tail", "head"}
            or not all(re.fullmatch(r"-?\d+|-n\d*", flag) for flag in suffix[1:])
        ):
            return None
        tokens = tokens[:split]
    if tokens[-3:] == ["2", ">&", "1"]:
        tokens = tokens[:-3]
    if any(
        token
        in {
            "\n",
            "--web",
            "--dry-run",
            ";",
            "&&",
            "||",
            "|",
            "&",
            "(",
            ")",
            ">",
            ">>",
            "<",
            "<<",
            ">&",
        }
        for token in tokens
    ):
        return None
    if tokens[:3] == ["gh", "pr", "create"]:
        return "github", tokens, environment
    if tokens[:3] == ["glab", "mr", "create"]:
        return "gitlab", tokens, environment
    return None


def _create_provider(command: str) -> str | None:
    invocation = _create_invocation(command)
    return invocation[0] if invocation else None


def _requested_github_repository(tokens: list[str], environment: dict[str, str]) -> str | None:
    """Read explicit repository selectors without mistaking body text for flags.

    None means cwd determines the repository; an empty value means an explicit
    selector was malformed or ambiguous and cannot support creation evidence.
    """
    repos = []
    value_flags = {
        "-a",
        "--assignee",
        "-B",
        "--base",
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-H",
        "--head",
        "-l",
        "--label",
        "-m",
        "--milestone",
        "-p",
        "--project",
        "--recover",
        "-r",
        "--reviewer",
        "-T",
        "--template",
        "-t",
        "--title",
    }
    i = 3
    while i < len(tokens):
        arg = tokens[i]
        if arg in {"-R", "--repo"}:
            if i + 1 >= len(tokens):
                return ""
            repos.append(tokens[i + 1])
            i += 2
        elif arg.startswith("--repo="):
            repos.append(arg.split("=", 1)[1])
            i += 1
        elif arg.startswith("-R") and len(arg) > 2:
            repos.append(arg[2:])
            i += 1
        elif arg in value_flags:
            i += 2
        else:
            i += 1
    # The explicit CLI flag overrides GH_REPO. Multiple flags are deliberately
    # not resolved by guessing the command-line parser's precedence.
    if len(repos) > 1:
        return ""
    selected = repos[0] if repos else environment.get("GH_REPO")
    if environment.get("GH_HOST", "github.com").lower() != "github.com":
        return ""
    if selected is None:
        return None
    selected = selected.removeprefix("github.com/")
    return selected if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", selected) else ""


def _command_sha256(command: str) -> str | None:
    try:
        return sha256(command.encode()).hexdigest()
    except UnicodeEncodeError:
        # JSON can contain unpaired surrogates. Preserve the observation while
        # leaving its byte digest unknown instead of hashing replacement bytes.
        return None


def is_pr_creation_tool(tool_name: str) -> bool:
    """Recognize the explicit GitHub MCP creation operation, not read tools."""
    return bool(
        re.fullmatch(
            r"(?:(?:functions|tools)\.)?(?:mcp__github__|github[._])create_pull_request", tool_name
        )
    )


def _github_api_creation_repo(command: str) -> str | None:
    """Recognize an explicit, single GitHub REST POST via gh api."""
    try:
        lexer = shlex.shlex(_literal_shell(command), posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    if tokens[:2] != ["gh", "api"]:
        return None
    method = None
    endpoint = None
    i = 2
    while i < len(tokens):
        token = tokens[i]
        if token in {
            "--method",
            "-X",
            "-f",
            "-F",
            "--field",
            "--raw-field",
            "--input",
            "-H",
            "--header",
        }:
            if i + 1 >= len(tokens):
                return None
            if token in {"--method", "-X"}:
                if method is not None:
                    return None
                method = tokens[i + 1]
            i += 2
        elif endpoint is None:
            endpoint = token
            i += 1
        else:
            return None
    match = re.fullmatch(
        r"(?:https://api\.github\.com/|/)?repos/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pulls",
        endpoint or "",
    )
    return match[1] if method == "POST" and match else None


def creation_command_attributes(value: object, tool_name: str = "") -> dict[str, str]:
    """Compute invocation evidence before parsers truncate long tool input.

    Retain the provider and full command digest, never another command/body copy.
    """
    if is_pr_creation_tool(tool_name):
        try:
            args = json.loads(value) if isinstance(value, str) else value
        except (ValueError, RecursionError):
            args = None
        if isinstance(args, dict):
            repo = f"{args.get('owner', '')}/{args.get('repo', '')}"
            if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                return {"pr.create.provider": "github", "pr.create.repository": repo}
        return {"pr.create.capture": "unrecognized_creation_text"}
    command = _command_value(value)
    api_repo = _github_api_creation_repo(command)
    if api_repo:
        digest = _command_sha256(command)
        return {
            "pr.create.provider": "github",
            "pr.create.repository": api_repo,
            **({"pr.create.command_sha256": digest} if digest else {}),
        }
    invocation = _create_invocation(command)
    provider = invocation[0] if invocation else None
    if not provider:
        return (
            {"pr.create.capture": "unrecognized_creation_text"}
            if re.search(r"\b(?:gh\s+pr|glab\s+mr)\s+create\b", command)
            else {}
        )
    requested = (
        _requested_github_repository(invocation[1], invocation[2]) if provider == "github" else None
    )
    digest = _command_sha256(command)
    return {
        "pr.create.provider": provider,
        **({"pr.create.requested_repository": requested} if requested is not None else {}),
        **({"pr.create.capture": "unrecognized_creation_text"} if requested == "" else {}),
        **({"pr.create.command_sha256": digest} if digest else {}),
    }


def _creation_match(output: object, provider: str) -> re.Match | None:
    if not isinstance(output, str):
        return None
    # A failed create often prints an existing PR URL. Inspect the full result,
    # including any error or second URL beyond a parser's display limit.
    if re.search(r"^Process exited with code [1-9]\d*$", output, re.MULTILINE):
        return None
    if re.search(r"already exists|failed|error:", output, re.IGNORECASE):
        return None
    matches = list((_GH_PR if provider == "github" else _GL_MR).finditer(output))
    if len(matches) != 1:
        return None
    match = matches[0]
    return match if match.group(0) in [line.strip() for line in output.splitlines()] else None


def creation_output_attributes(
    output: object, provider: str | None, repository: str | None = None
) -> dict[str, str]:
    """Retain validated URL evidence before display truncation, not the raw output."""
    if provider not in {"github", "gitlab"}:
        return {}
    if repository is not None:
        return {"pr.create.output_url": _structured_creation_url(output, repository)}
    match = _creation_match(output, provider)
    # An explicit empty result prevents falling back to a truncated prefix that
    # may hide a later error or another URL. Missing metadata remains legacy.
    return {"pr.create.output_url": match.group(0) if match else ""}


def _structured_creation_url(output: object, repository: str) -> str:
    """Validate one successful MCP creation response against its request repo."""
    try:
        value = json.loads(output) if isinstance(output, str) else output
        if isinstance(value, dict) and "content" in value:
            if value.get("isError") is not None and value.get("isError") is not False:
                return ""
            value = value["content"]
            if isinstance(value, str):
                value = json.loads(value)
        if isinstance(value, list):
            if len(value) != 1 or not isinstance(value[0], dict) or value[0].get("type") != "text":
                return ""
            value = json.loads(value[0]["text"])
        if not isinstance(value, dict) or value.get("error") or value.get("isError"):
            return ""
        url = value.get("html_url") or value.get("url")
        match = _GH_PR.fullmatch(url) if isinstance(url, str) else None
        if not match or match[1].lower() != repository.lower() or int(match[2]) <= 0:
            return ""
        number = value.get("number")
        if number is not None and (type(number) is not int or number != int(match[2])):
            return ""
        return url
    except (ValueError, TypeError, KeyError, RecursionError):
        return ""


def extract_created_prs(spans: list[SpanData]) -> list[CreatedPR]:
    """Return only successful direct creates with one unambiguous output URL."""
    turns = {}
    for span in spans:
        match = re.fullmatch(r"Turn (\d+)", span.name)
        if match:
            turns[span.span_id] = int(match[1])
    seen: set[str] = set()
    result = []
    for span in spans:
        if span.status_code != "OK":
            continue
        structured = span.attributes.get("pr.create.repository")
        if (span.tool_name or "").lower() not in _SHELL_TOOLS and not (
            structured and is_pr_creation_tool(span.tool_name or "")
        ):
            continue
        command = command_text(span)
        provider = span.attributes.get("pr.create.provider") or _create_provider(command)
        if provider not in {"github", "gitlab"}:
            continue
        output = span.attributes.get("pr.create.output_url", span.output_value)
        match = _creation_match(output, provider)
        if match is None:
            continue
        if provider == "github":
            repo, number = match.groups()
            url = f"https://github.com/{repo}/pull/{number}"
        else:
            host, repo, number = match.groups()
            url = f"https://{host}/{repo}/-/merge_requests/{number}"
        requested = span.attributes.get("pr.create.requested_repository")
        if requested is None and provider == "github":
            invocation = _create_invocation(command)
            requested = (
                _requested_github_repository(invocation[1], invocation[2]) if invocation else None
            )
        if requested is not None and (not requested or requested.lower() != repo.lower()):
            continue
        if url in seen or int(number) <= 0:
            continue
        seen.add(url)
        digest = span.attributes.get("pr.create.command_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            # Only the parser saw the complete source command. An unmarked
            # historical span may contain a valid-looking truncated prefix.
            digest = None
        result.append(
            CreatedPR(
                provider=provider,
                repo_name=repo,
                number=int(number),
                url=url,
                source_event_id=span.span_id,
                command_sha256=digest,
                created_in_turn=turns.get(span.parent_id),
            )
        )
    return result
