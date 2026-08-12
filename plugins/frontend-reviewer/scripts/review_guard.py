#!/usr/bin/env python3
"""Enforce read-only worktree access for active frontend-review agents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any


CONFIG_ENV = "MY_FLOW_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))
SESSION_DIRECTORY = CONFIG_PATH.parent / "review-sessions"
REVIEWS_DIRECTORY = ".reviews"
MUTATING_TOOL_MARKERS = ("write", "edit", "apply_patch", "delete", "move", "rename", "exec_command", "bash", "shell")
BLOCKED_SOURCE_MARKERS = ("web__", "search", "http", "fetch", "curl", "wget", "connector", "github", "gitlab")
READ_ONLY_COMMANDS = {
    "awk", "cut", "file", "find", "git", "grep", "head", "ls", "pwd", "readlink", "realpath", "rg", "sed",
    "sort", "stat", "tail", "tr", "uniq", "wc",
}
READ_ONLY_GIT_COMMANDS = {"diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}
DANGEROUS_SHELL_ARGUMENTS = {
    "--exec", "--exec-path", "--open-files-in-pager", "--output", "--pre", "--pre-glob", "--write",
    "-delete", "-exec", "-execdir", "-fls", "-fprint", "-fprintf", "-i", "-o",
}


def main() -> int:
    payload = read_payload()
    session = select_active_session(payload)
    if session is None:
        return 0
    worktree = Path(session["worktree"]).resolve()
    reviews = worktree / REVIEWS_DIRECTORY
    tool_name = get_tool_name(payload).lower()
    tool_input = get_tool_input(payload)

    if is_blocked_source(tool_name, tool_input, worktree):
        return block("Frontend review agents may only read the designated worktree; external web and connector access is blocked.")

    workdir = find_workdir(payload)
    base = workdir or worktree
    if workdir is not None and not is_within(workdir, worktree):
        return block(f"Frontend review agents must keep workdir inside {worktree}: {workdir}")

    for path in explicit_paths(tool_input, base):
        if not is_within(path, worktree):
            return block(f"Frontend review agents may not read outside {worktree}: {path}")

    if is_shell_tool(tool_name):
        command = first_string(tool_input, ("cmd", "command", "script"))
        if not command:
            return block("Frontend review shell calls require an explicit command.")
        if is_review_session_control(command, worktree):
            return 0
        violation = validate_read_only_shell(command, base, worktree)
        return block(violation) if violation else 0

    if "apply_patch" in tool_name:
        patch = tool_input if isinstance(tool_input, str) else first_string(tool_input, ("patch", "cmd", "command")) or ""
        paths = patch_paths(patch, base)
        if not paths or any(not is_within(path, reviews) for path in paths):
            return block(f"Frontend review agents may patch only inside {reviews}.")
        return 0

    if any(marker in tool_name for marker in MUTATING_TOOL_MARKERS):
        paths = explicit_paths(tool_input, base)
        if not paths or any(not is_within(path, reviews) for path in paths):
            return block(f"Frontend review agents may write only inside {reviews}.")
    return 0


def select_active_session(payload: Any) -> dict[str, Any] | None:
    target = load_target()
    if target is None:
        return None
    sessions: list[dict[str, Any]] = []
    worktrees = (target / "worktrees").resolve()
    if not worktrees.is_dir() or not SESSION_DIRECTORY.is_dir():
        return None
    for path in SESSION_DIRECTORY.glob("*.json"):
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_worktree = session.get("worktree") if isinstance(session, dict) else None
        if not isinstance(raw_worktree, str):
            continue
        worktree = Path(raw_worktree).expanduser().resolve()
        if worktree.parent != worktrees or path.resolve() != review_session_path(worktree).resolve():
            continue
        if session.get("active") is True and session.get("worktree") == str(worktree):
            sessions.append(session)
    if not sessions:
        return None
    workdir = find_workdir(payload)
    if workdir is not None:
        matches = [session for session in sessions if is_within(workdir, Path(session["worktree"]).resolve())]
        if len(matches) == 1:
            return matches[0]
    return sessions[0] if len(sessions) == 1 else None


def validate_read_only_shell(command: str, base: Path, worktree: Path) -> str | None:
    if re.search(r"(?:^|[^<])(?:>>?|2>|&>)|\$\(|`", command):
        return "Frontend review shell commands must be read-only and may not redirect output or use command substitution."
    for argv in shell_words(command):
        if not argv:
            continue
        executable = Path(argv[0]).name
        if executable not in READ_ONLY_COMMANDS:
            return f"Frontend review shell command is not in the read-only allowlist: {executable}"
        for argument in argv[1:]:
            option = argument.split("=", 1)[0]
            if option in DANGEROUS_SHELL_ARGUMENTS or option.startswith("--output"):
                return f"Frontend review shell argument may execute or write and is blocked: {option}"
        if executable == "awk" and any("system(" in argument or "getline" in argument for argument in argv[1:]):
            return "Frontend review awk commands may not execute subprocesses or open secondary input."
        if executable == "sed" and any(re.search(r"(^|[^A-Za-z])e([^A-Za-z]|$)", argument) for argument in argv[1:]):
            return "Frontend review sed commands may not execute subprocesses."
        if executable == "git":
            subcommand = next((item for item in argv[1:] if not item.startswith("-")), "")
            if subcommand not in READ_ONLY_GIT_COMMANDS:
                return f"Frontend review agents may not run mutating git command: {subcommand or '(missing)'}"
        for token in argv[1:]:
            path = token_path(token, base)
            if path is not None and not is_within(path, worktree):
                return f"Frontend review shell command references a path outside {worktree}: {path}"
    return None


def is_review_session_control(command: str, worktree: Path) -> bool:
    if re.search(r"(?:&&|\|\||[;|<>`])|\$\(", command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if len(argv) < 4 or Path(argv[0]).name not in {"python", "python3"}:
        return False
    if Path(argv[1]).name != "review_session.py" or Path(argv[3]).expanduser().resolve() != worktree:
        return False
    if argv[2] == "validate":
        return (
            len(argv) == 8
            and argv[4] == "--round"
            and argv[5].isdigit()
            and argv[6] == "--token"
            and bool(argv[7])
        )
    if argv[2] == "stop":
        return len(argv) == 6 and argv[4] == "--token" and bool(argv[5])
    return False


def is_blocked_source(tool_name: str, tool_input: Any, worktree: Path) -> bool:
    if any(marker in tool_name for marker in BLOCKED_SOURCE_MARKERS):
        return True
    if tool_name.startswith("mcp__") and not any(marker in tool_name for marker in ("filesystem", "file_system", "mcp__fs__")):
        return True
    if "browser" not in tool_name and "chrome" not in tool_name:
        return False
    urls = re.findall(r"(?:https?|file)://[^\s\"']+", " ".join(extract_text(tool_input)))
    for url in urls:
        if url.startswith(("http://localhost", "http://127.0.0.1")):
            continue
        if url.startswith("file://") and is_within(Path(url[7:]).expanduser().resolve(), worktree):
            continue
        return True
    return False


def patch_paths(patch: str, base: Path) -> list[Path]:
    paths: list[Path] = []
    for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE):
        paths.append(resolve_path(match.group(1).strip(), base))
    return paths


def explicit_paths(value: Any, base: Path) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"path", "file", "filename", "directory", "workdir", "cwd"} and isinstance(item, str):
                paths.append(resolve_path(item, base))
            elif key.lower() not in {"cmd", "command", "script", "patch"}:
                paths.extend(explicit_paths(item, base))
    elif isinstance(value, list):
        for item in value:
            paths.extend(explicit_paths(item, base))
    return paths


def token_path(token: str, base: Path) -> Path | None:
    if token in {".", ".."}:
        return resolve_path(token, base)
    if token.startswith(("/", "./", "../")):
        return resolve_path(token, base)
    return None


def resolve_path(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def shell_words(text: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for part in re.split(r"\s*(?:&&|\|\||\||;|\n)\s*", text):
        if not part.strip():
            continue
        try:
            commands.append(shlex.split(part))
        except ValueError:
            commands.append(part.split())
    return commands


def load_target() -> Path | None:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw_target = payload.get("targetWorkspace")
    except (OSError, json.JSONDecodeError):
        return None
    return Path(raw_target).expanduser().resolve() if isinstance(raw_target, str) and raw_target else None


def review_session_path(worktree: Path) -> Path:
    digest = hashlib.sha256(str(worktree.resolve()).encode("utf-8")).hexdigest()
    return SESSION_DIRECTORY / f"{digest}.json"


def read_payload() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def get_tool_name(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("tool_name", "toolName", "name", "recipient_name"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return ""


def get_tool_input(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("tool_input", "toolInput", "input", "parameters"):
            if key in payload:
                return payload[key]
    return payload


def find_workdir(payload: Any) -> Path | None:
    for candidate in (payload, get_tool_input(payload)):
        if isinstance(candidate, dict):
            value = first_string(candidate, ("workdir", "cwd", "workingDirectory", "working_directory"))
            if value:
                return Path(value).expanduser().resolve()
    return None


def first_string(payload: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def extract_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in extract_text(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in extract_text(item)]
    return []


def is_shell_tool(tool_name: str) -> bool:
    return "bash" in tool_name or "shell" in tool_name or "exec_command" in tool_name


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    print(reason, file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
