#!/usr/bin/env python3
"""Install My Flow Git hooks into managed worktrees."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


CONFIG_ENV = "MY_FLOW_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOKS_PATH = PLUGIN_ROOT / "git-hooks"


def main() -> int:
    target = load_target()
    if target is None:
        print(json.dumps({"installed": [], "failed": []}))
        return 0

    worktrees = target / "worktrees"
    if not worktrees.is_dir():
        print(json.dumps({"installed": [], "failed": []}))
        return 0

    installed: list[str] = []
    failed: list[dict[str, str]] = []
    for worktree in sorted(path for path in worktrees.iterdir() if path.is_dir()):
        if not is_git_worktree(worktree):
            continue
        result = subprocess.run(
            ["git", "-C", str(worktree), "config", "core.hooksPath", str(HOOKS_PATH)],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            installed.append(worktree.name)
        else:
            detail = result.stderr.strip() or result.stdout.strip() or f"git exited {result.returncode}"
            failed.append({"worktree": worktree.name, "error": detail})

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


def is_git_worktree(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


if __name__ == "__main__":
    raise SystemExit(main())
