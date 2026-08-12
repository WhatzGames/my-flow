#!/usr/bin/env python3
"""Tests for marketplace-wide My Flow workspace enforcement."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOKS_PATH = PLUGIN_ROOT / "hooks.json"
GUARD_SCRIPT = PLUGIN_ROOT / "scripts" / "workspace_guard.py"


class WorkspaceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "target"
        self.worktree = self.target / "worktrees" / "example-main"
        self.worktree.mkdir(parents=True)
        self.environment = os.environ.copy()
        self.environment["MY_FLOW_CONFIG"] = str(self.root / "config.json")
        Path(self.environment["MY_FLOW_CONFIG"]).write_text(
            json.dumps({"targetWorkspace": str(self.target), "sshHost": "github-test"}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def guard(self, tool_name: str, tool_input: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(GUARD_SCRIPT)],
            input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )

    def test_guard_runs_before_every_tool(self) -> None:
        hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hooks), {"PreToolUse"})
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], ".*")
        self.assertEqual(hooks["PreToolUse"][0]["hooks"][0]["command"], "./scripts/workspace_guard.py")

    def test_shell_workdir_must_stay_in_managed_worktrees(self) -> None:
        inside = self.guard("functions.exec_command", {"cmd": "pwd", "workdir": str(self.worktree)})
        outside = self.guard("functions.exec_command", {"cmd": "pwd", "workdir": str(self.target)})
        escape = self.guard("functions.exec_command", {"cmd": "cd ../..", "workdir": str(self.worktree)})
        self.assertEqual(inside.returncode, 0, inside.stderr)
        self.assertEqual(outside.returncode, 2)
        self.assertEqual(escape.returncode, 2)

    def test_mutating_paths_must_stay_in_managed_worktrees(self) -> None:
        inside = self.guard("Write", {"path": str(self.worktree / "result.txt")})
        outside = self.guard("Write", {"path": str(self.target / "result.txt")})
        self.assertEqual(inside.returncode, 0, inside.stderr)
        self.assertEqual(outside.returncode, 2)

    def test_unconfigured_workspace_is_blocked(self) -> None:
        Path(self.environment["MY_FLOW_CONFIG"]).unlink()
        result = self.guard("Read", {"path": str(self.worktree / "README.md")})
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires a configured target workspace", result.stderr)

    def test_unconfigured_workspace_allows_only_exact_my_flow_setup_script(self) -> None:
        Path(self.environment["MY_FLOW_CONFIG"]).unlink()
        setup_script = PLUGIN_ROOT.parent / "my-flow" / "scripts" / "target_workspace.py"
        allowed = self.guard(
            "functions.exec_command",
            {"cmd": f"python3 {setup_script} set {self.target}", "workdir": str(self.root)},
        )
        wrong_script = self.guard(
            "functions.exec_command",
            {"cmd": f"python3 {self.root / 'target_workspace.py'} status", "workdir": str(self.root)},
        )
        chained = self.guard(
            "functions.exec_command",
            {"cmd": f"python3 {setup_script} status && touch escaped", "workdir": str(self.root)},
        )
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual(wrong_script.returncode, 2)
        self.assertEqual(chained.returncode, 2)


if __name__ == "__main__":
    unittest.main()
