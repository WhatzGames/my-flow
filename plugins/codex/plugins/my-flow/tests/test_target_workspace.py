#!/usr/bin/env python3
"""Integration tests for the My Flow workspace lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "target_workspace.py"
ACCESS_SCRIPT = PLUGIN_ROOT / "scripts" / "allow_worktree_access.py"
SSH_SCRIPT = PLUGIN_ROOT / "scripts" / "enforce_ssh_host.py"
PUSH_SCRIPT = PLUGIN_ROOT / "scripts" / "require_github_push_approval.py"
INSTALL_HOOKS_SCRIPT = PLUGIN_ROOT / "scripts" / "install_git_hooks.py"
COMMIT_TRAILER = "Co-authored-by: GPT-5 <noreply@openai.com>"


class TargetWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_directory.name)
        self.target = self.root / "target"
        self.target.mkdir()
        self.environment = os.environ.copy()
        self.environment["MY_FLOW_CONFIG"] = str(self.root / "config.json")

    def tearDown(self) -> None:
        self.temp_directory.cleanup()

    def run_script(self, *arguments: str, payload: dict | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SCRIPT), *arguments],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )

    def run_access_hook(self, payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(ACCESS_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )

    def run_ssh_hook(self, payload: dict) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SSH_SCRIPT)],
            input=json.dumps(payload),
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )

    def run_install_hooks(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(INSTALL_HOOKS_SCRIPT)],
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )

    def configure(self, ssh_host: str = "github-work") -> None:
        self.assertEqual(self.run_script("set", str(self.target)).returncode, 0)
        self.assertEqual(self.run_script("set-host", ssh_host).returncode, 0)

    def test_startup_creates_target_from_environment_path(self) -> None:
        startup_target = self.root / "startup" / "codex-projects"
        self.environment["MY_FLOW_STARTUP_PATH"] = str(startup_target)

        started = self.run_script("startup")
        self.assertEqual(started.returncode, 0, started.stderr)
        payload = json.loads(started.stdout)
        self.assertEqual(payload["targetWorkspace"], str(startup_target.resolve()))
        self.assertEqual(payload["sshHost"], None)
        self.assertEqual(payload["bares"], str((startup_target / "bares").resolve()))
        self.assertEqual(payload["worktrees"], str((startup_target / "worktrees").resolve()))
        self.assertTrue(payload["created"])
        self.assertTrue((startup_target / "bares").is_dir())
        self.assertTrue((startup_target / "worktrees").is_dir())

        status = self.run_script("status")
        self.assertEqual(status.returncode, 1)
        self.assertEqual(json.loads(status.stdout)["targetWorkspace"], str(startup_target.resolve()))

    def test_startup_uses_payload_cwd_when_environment_path_is_unset(self) -> None:
        startup_target = self.root / "payload-target"

        started = self.run_script("startup", payload={"cwd": str(startup_target)})
        self.assertEqual(started.returncode, 0, started.stderr)
        payload = json.loads(started.stdout)
        self.assertEqual(payload["targetWorkspace"], str(startup_target.resolve()))
        self.assertTrue((startup_target / "bares").is_dir())
        self.assertTrue((startup_target / "worktrees").is_dir())

    def test_set_creates_layout_and_refreshes_bare_repository(self) -> None:
        configured = self.run_script("set", str(self.target))
        self.assertEqual(configured.returncode, 0, configured.stderr)
        host_configured = self.run_script("set-host", "github-work")
        self.assertEqual(host_configured.returncode, 0, host_configured.stderr)
        self.assertTrue((self.target / "bares").is_dir())
        self.assertTrue((self.target / "worktrees").is_dir())

        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
        seed = self.root / "seed"
        subprocess.run(["git", "init", str(seed)], check=True, capture_output=True, text=True)
        (seed / "README.md").write_text("# Example\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-C", str(seed), "-c", "user.name=My Flow Test", "-c", "user.email=my-flow@example.invalid", "commit", "-m", "Initial commit"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(seed), "push", "origin", "HEAD:refs/heads/main"], check=True, capture_output=True, text=True)
        subprocess.run(["git", f"--git-dir={remote}", "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        managed = self.target / "bares" / "example.git"
        subprocess.run(["git", "clone", "--bare", str(remote), str(managed)], check=True, capture_output=True, text=True)

        refreshed = self.run_script("refresh")
        self.assertEqual(refreshed.returncode, 0, f"{refreshed.stderr}\n{refreshed.stdout}")
        self.assertEqual(json.loads(refreshed.stdout)["fetched"], ["example.git"])
        remote_head = subprocess.run(
            ["git", f"--git-dir={remote}", "rev-parse", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        fetched_head = subprocess.run(
            ["git", f"--git-dir={managed}", "rev-parse", "refs/remotes/origin/main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(fetched_head, remote_head)

    def test_hook_allows_worktrees_and_blocks_target_root(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        checkout.mkdir()

        inside = self.run_script(
            "hook",
            payload={"tool_name": "functions.exec_command", "tool_input": {"cmd": "pwd", "workdir": str(checkout)}},
        )
        outside = self.run_script(
            "hook",
            payload={"tool_name": "functions.exec_command", "tool_input": {"cmd": "pwd", "workdir": str(self.target)}},
        )

        self.assertEqual(inside.returncode, 0, inside.stderr)
        self.assertEqual(outside.returncode, 2)
        self.assertEqual(json.loads(outside.stdout)["decision"], "block")

    def test_hook_allows_wrapped_workdir_inside_worktree(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        checkout.mkdir()

        wrapped = self.run_script(
            "hook",
            payload={
                "tool_name": "functions.exec_command",
                "tool_input": {"arguments": {"cmd": "git status --short", "workdir": str(checkout)}},
            },
        )

        self.assertEqual(wrapped.returncode, 0, wrapped.stderr)

    def test_hook_installs_git_hooks_for_late_created_worktree(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True, text=True)

        before = subprocess.run(
            ["git", "-C", str(checkout), "config", "core.hooksPath"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertNotEqual(before.stdout.strip(), str(PLUGIN_ROOT / "git-hooks"))

        hooked = self.run_script(
            "hook",
            payload={"tool_name": "functions.exec_command", "tool_input": {"cmd": "git status", "workdir": str(checkout)}},
        )
        self.assertEqual(hooked.returncode, 0, hooked.stderr)
        hooks_path = subprocess.run(
            ["git", "-C", str(checkout), "config", "core.hooksPath"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(hooks_path, str(PLUGIN_ROOT / "git-hooks"))

    def test_access_hook_auto_allows_routine_worktree_tools(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        checkout.mkdir()

        for tool_name in ("apply_patch", "mcp__browser__navigate"):
            result = self.run_access_hook(
                {"cwd": str(checkout), "tool_name": tool_name, "tool_input": {"command": "write a file"}}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            decision = json.loads(result.stdout)["hookSpecificOutput"]["decision"]
            self.assertEqual(decision, {"behavior": "allow"})

    def test_access_hook_defers_sensitive_requests(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        checkout.mkdir()
        payloads = (
            {"cwd": str(self.target), "tool_name": "apply_patch", "tool_input": {"command": "write"}},
            {"cwd": str(checkout), "tool_name": "Bash", "tool_input": {"command": "rm -rf build"}},
            {"cwd": str(checkout), "tool_name": "Bash", "tool_input": {"command": "git push origin main"}},
            {
                "cwd": str(checkout),
                "tool_name": "Write",
                "tool_input": {"path": str(self.target / "bares" / "example.git"), "command": "write"},
            },
            {"cwd": str(checkout), "tool_name": "mcp__payments__refund", "tool_input": {}},
        )

        for payload in payloads:
            result = self.run_access_hook(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")

    def test_hook_blocks_absolute_patch_path_outside_worktrees(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        checkout.mkdir()
        result = self.run_script(
            "hook",
            payload={
                "tool_name": "apply_patch",
                "tool_input": {
                    "command": f"*** Begin Patch\n*** Add File: {self.target / 'outside.txt'}\n+no\n*** End Patch"
                },
            },
        )
        self.assertEqual(result.returncode, 2)

    def test_hook_requires_ssh_host_once_after_target(self) -> None:
        self.assertEqual(self.run_script("set", str(self.target)).returncode, 0)
        checkout = self.target / "worktrees" / "example-main"
        checkout.mkdir()
        payload = {"tool_name": "functions.exec_command", "tool_input": {"cmd": "pwd", "workdir": str(checkout)}}

        missing = self.run_script("hook", payload=payload)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("SSH Host alias", missing.stderr)

        self.assertEqual(self.run_script("set-host", "github-work").returncode, 0)
        configured = self.run_script("hook", payload=payload)
        self.assertEqual(configured.returncode, 0, configured.stderr)

    def test_ssh_hook_allows_only_configured_alias(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        checkout.mkdir()

        allowed_commands = (
            "ssh github-work",
            "git clone git@github-work:OWNER/project.git",
            "git push origin HEAD:refs/heads/main",
        )
        blocked_commands = (
            "ssh github.com",
            "ssh -o HostName=github.com github-work",
            "ssh -F /tmp/other-config github-work",
            "git clone git@github.com:OWNER/project.git",
            "git clone https://github.com/OWNER/project.git",
            "GIT_SSH_COMMAND='ssh github.com' git fetch",
            "git -c core.sshCommand='ssh github.com' fetch",
            "gh repo clone OWNER/project",
        )
        for command in allowed_commands:
            result = self.run_ssh_hook(
                {"tool_name": "Bash", "tool_input": {"command": command, "workdir": str(checkout)}}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        for command in blocked_commands:
            result = self.run_ssh_hook(
                {"tool_name": "Bash", "tool_input": {"command": command, "workdir": str(checkout)}}
            )
            self.assertEqual(result.returncode, 2, command)

    def test_refresh_rejects_bare_remote_on_another_host(self) -> None:
        self.configure()
        managed = self.target / "bares" / "blocked.git"
        subprocess.run(["git", "init", "--bare", str(managed)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", f"--git-dir={managed}", "remote", "add", "origin", "git@github.com:OWNER/project.git"],
            check=True,
        )

        refreshed = self.run_script("refresh")
        self.assertEqual(refreshed.returncode, 1)
        failure = json.loads(refreshed.stdout)["failed"][0]
        self.assertEqual(failure["repository"], "blocked.git")
        self.assertIn("instead of `github-work`", failure["error"])

    def test_environment_prefixed_push_still_requires_approval(self) -> None:
        result = subprocess.run(
            ["python3", str(PUSH_SCRIPT)],
            input=json.dumps(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "GIT_SSH_COMMAND='ssh -o BatchMode=yes' git push origin main"},
                }
            ),
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("git push", result.stderr)

    def test_installed_commit_hook_adds_codex_coauthor(self) -> None:
        self.configure()
        checkout = self.target / "worktrees" / "example-main"
        subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True, text=True)

        installed = self.run_install_hooks()
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.assertEqual(json.loads(installed.stdout)["installed"], ["example-main"])
        hooks_path = subprocess.run(
            ["git", "-C", str(checkout), "config", "core.hooksPath"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(hooks_path, str(PLUGIN_ROOT / "git-hooks"))

        (checkout / "README.md").write_text("# Example\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(checkout), "add", "README.md"], check=True, capture_output=True, text=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "-c",
                "user.name=My Flow Test",
                "-c",
                "user.email=my-flow@example.invalid",
                "commit",
                "-m",
                "Initial commit",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        message = subprocess.run(
            ["git", "-C", str(checkout), "log", "-1", "--format=%B"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn(COMMIT_TRAILER, message)


if __name__ == "__main__":
    unittest.main()
