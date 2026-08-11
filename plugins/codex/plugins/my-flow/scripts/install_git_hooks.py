#!/usr/bin/env python3
"""Install My Flow Git hooks into managed worktrees."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from target_workspace import install_git_hooks, is_git_worktree


CONFIG_ENV = "MY_FLOW_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))


def main() -> int:
    target = load_target()
    if target is None:
        print(json.dumps({"installed": [], "failed": []}))
        return 0

    worktrees = target / "worktrees"
    if not worktrees.is_dir():
        print(json.dumps({"installed": [], "failed": []}))
        return 0

    installed = [
        worktree.name
        for worktree in sorted(path for path in worktrees.iterdir() if path.is_dir() and is_git_worktree(path))
    ]
    failed = install_git_hooks(target)

    print(json.dumps({"installed": installed, "failed": failed}))
    if failed:
        print("My Flow could not install Git hooks into every worktree.", file=sys.stderr)
        return 1
    return 0


def load_target() -> Path | None:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("targetWorkspace")
    if not isinstance(value, str) or not value:
        return None
    return Path(value).expanduser().resolve()


if __name__ == "__main__":
    raise SystemExit(main())
