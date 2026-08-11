#!/usr/bin/env python3
"""Auto-allow non-destructive access requests inside managed worktrees."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from require_github_push_approval import classify_publish_attempt, extract_text


CONFIG_ENV = "MY_FLOW_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))
DESTRUCTIVE_SHELL_PATTERN = re.compile(
    r"(?:^|[;&|]\s*)(?:sudo\s+)?(?:rm|rmdir|unlink|shred|mkfs(?:\.\w+)?|diskutil\s+erase|"
    r"git\s+(?:clean\b|reset\s+--hard\b))",
    re.IGNORECASE,
)


def main() -> int:
    payload = read_payload()
    target = load_target()
    if target is None:
        return 0

    worktrees = target / "worktrees"
    cwd = find_cwd(payload)
    if cwd is None or not is_within(cwd, worktrees):
        return 0

    texts = list(extract_text(payload))
    if classify_publish_attempt(texts) is not None:
        return 0

    tool_name = get_tool_name(payload).lower()
    if not is_routine_worktree_tool(tool_name):
        return 0
    if is_shell_tool(tool_name) and any(DESTRUCTIVE_SHELL_PATTERN.search(text) for text in texts):
        return 0

    if any(not is_within(path, worktrees) for path in explicit_absolute_paths(payload)):
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }
        )
    )
    return 0


def read_payload() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


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


def find_cwd(payload: Any) -> Path | None:
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for key in ("tool_input", "toolInput", "input", "parameters"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        for key in ("cwd", "workdir", "workingDirectory", "working_directory"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return Path(value).expanduser().resolve()
    return None


def get_tool_name(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("tool_name", "toolName", "name", "recipient_name"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
    return ""


def explicit_absolute_paths(value: Any) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"path", "file", "filename", "directory"} and isinstance(item, str) and item.startswith("/"):
                paths.append(Path(item).expanduser().resolve())
            else:
                paths.extend(explicit_absolute_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(explicit_absolute_paths(item))
    return paths


def is_shell_tool(tool_name: str) -> bool:
    return "bash" in tool_name or "shell" in tool_name or "exec_command" in tool_name


def is_routine_worktree_tool(tool_name: str) -> bool:
    if is_shell_tool(tool_name) or tool_name in {"apply_patch", "edit", "write", "read"}:
        return True
    return any(
        marker in tool_name
        for marker in ("browser", "chrome", "playwright", "web", "filesystem", "file_system", "mcp__fs__")
    )


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


if __name__ == "__main__":
    raise SystemExit(main())
