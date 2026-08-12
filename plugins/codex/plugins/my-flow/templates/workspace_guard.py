#!/usr/bin/env python3
"""Keep plugin implementation work inside the configured My Flow worktrees."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any


CONFIG_ENV = "MY_FLOW_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MUTATING_TOOL_MARKERS = (
    "write",
    "edit",
    "apply_patch",
    "multi_edit",
    "delete",
    "move",
    "rename",
    "exec_command",
    "bash",
    "shell",
)


def main() -> int:
    payload = read_payload()
    if is_safe_target_workspace_command(payload):
        return 0
    target = load_target()
    if target is None:
        return block(
            "This My Flow marketplace plugin requires a configured target workspace. "
            "Install and configure the my-flow plugin before using it."
        )

    worktrees = (target / "worktrees").resolve()
    tool_name = get_tool_name(payload).lower()
    tool_input = get_tool_input(payload)
    workdir = find_workdir(payload)

    if workdir is not None and not is_within(workdir, worktrees):
        return block(f"My Flow blocked workdir `{workdir}` outside managed worktrees directory `{worktrees}`.")

    if is_shell_tool(tool_name):
        if workdir is None:
            return block(f"My Flow requires shell commands to set workdir inside `{worktrees}`.")
        command = first_string(tool_input, ("cmd", "command", "script"))
        if command:
            violation = check_cd_targets(command, workdir, worktrees)
            if violation:
                return block(violation)

    if any(marker in tool_name for marker in MUTATING_TOOL_MARKERS):
        base = workdir or worktrees
        for path in explicit_paths(tool_input, base):
            if not is_within(path, worktrees):
                return block(
                    f"My Flow blocked implementation work outside `{worktrees}`. "
                    f"Path `{path}` is not in the managed worktrees directory."
                )
        if "apply_patch" in tool_name:
            patch = tool_input if isinstance(tool_input, str) else first_string(tool_input, ("patch", "command", "cmd")) or ""
            for path in patch_paths(patch, base):
                if not is_within(path, worktrees):
                    return block(f"My Flow blocked a patch outside managed worktrees directory `{worktrees}`: `{path}`.")

    return 0


def load_target() -> Path | None:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        raw_target = payload.get("targetWorkspace")
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw_target, str) or not raw_target:
        return None
    target = Path(raw_target).expanduser().resolve()
    return target if target.is_dir() else None


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
        raw = first_string(candidate, ("workdir", "cwd", "workingDirectory", "working_directory"))
        if raw:
            return Path(raw).expanduser().resolve()
    return None


def explicit_paths(value: Any, base: Path) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"path", "file", "filename", "directory"} and isinstance(item, str):
                paths.append(resolve_path(item, base))
            elif key.lower() not in {"cmd", "command", "script", "patch"}:
                paths.extend(explicit_paths(item, base))
    elif isinstance(value, list):
        for item in value:
            paths.extend(explicit_paths(item, base))
    return paths


def patch_paths(patch: str, base: Path) -> list[Path]:
    return [
        resolve_path(match.group(1).strip(), base)
        for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
    ]


def is_safe_target_workspace_command(payload: Any) -> bool:
    tool_name = get_tool_name(payload).lower()
    if not is_shell_tool(tool_name):
        return False
    tool_input = get_tool_input(payload)
    command = first_string(tool_input, ("cmd", "command", "script"))
    workdir = find_workdir(payload)
    if not command or workdir is None or re.search(r"(?:&&|\|\||[;|<>`]|\$\()", command):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if argv and Path(argv[0]).name in {"python", "python3"}:
        argv = argv[1:]
    if len(argv) not in {2, 3}:
        return False
    script = resolve_path(argv[0], workdir)
    if script not in allowed_target_workspace_scripts():
        return False
    subcommand = argv[1]
    if subcommand in {"status", "startup", "clear", "refresh"}:
        return len(argv) == 2
    return subcommand in {"set", "set-host"} and len(argv) == 3


def allowed_target_workspace_scripts() -> set[Path]:
    candidates: set[Path] = set()
    installed = Path.home() / ".codex" / "plugins" / "cache" / "my-flow" / "my-flow"
    if installed.is_dir():
        candidates.update(path.resolve() for path in installed.glob("*/scripts/target_workspace.py") if path.is_file())
    for ancestor in PLUGIN_ROOT.parents:
        marketplace = ancestor / ".agents" / "plugins" / "marketplace.json"
        if not marketplace.is_file():
            continue
        try:
            manifest = json.loads(marketplace.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for entry in manifest.get("plugins", []):
            if not isinstance(entry, dict) or entry.get("name") != "my-flow":
                continue
            source = entry.get("source")
            raw_path = source.get("path") if isinstance(source, dict) else None
            if isinstance(raw_path, str):
                script = (ancestor / raw_path / "scripts" / "target_workspace.py").resolve()
                if script.is_file():
                    candidates.add(script)
        break
    return candidates


def check_cd_targets(command: str, workdir: Path, worktrees: Path) -> str | None:
    for part in re.split(r"\s*(?:&&|\|\||;|\n)\s*", command):
        if not part.strip():
            continue
        try:
            argv = shlex.split(part)
        except ValueError:
            argv = part.split()
        if argv[:1] == ["cd"] and len(argv) >= 2:
            path = resolve_path(argv[1], workdir)
            if not is_within(path, worktrees):
                return f"My Flow blocked `cd {argv[1]}` because it leaves `{worktrees}`."
    return None


def first_string(payload: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def resolve_path(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


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
