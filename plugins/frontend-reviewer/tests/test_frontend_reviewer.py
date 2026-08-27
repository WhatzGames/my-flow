#!/usr/bin/env python3
"""Behavior tests for guarded frontend-review sessions."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HOOKS_PATH = PLUGIN_ROOT / "hooks" / "hooks.json"
SESSION_SCRIPT = PLUGIN_ROOT / "scripts" / "review_session.py"
GUARD_SCRIPT = PLUGIN_ROOT / "scripts" / "review_guard.py"


def finding(identifier: str, status: str = "open", risk: str = "") -> dict[str, str]:
    return {
        "id": identifier,
        "severity": "medium",
        "status": status,
        "summary": f"Concise finding {identifier}",
        "evidence": "src/App.tsx:10",
        "recommendation": "Apply a focused improvement.",
        "regression_risk": risk,
    }


class FrontendReviewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "target"
        self.worktrees = self.target / "worktrees"
        self.worktree = self.worktrees / "example-main"
        self.worktree.mkdir(parents=True)
        (self.worktree / "src").mkdir()
        (self.worktree / "src" / "App.tsx").write_text("export default function App() {}\n", encoding="utf-8")
        self.environment = os.environ.copy()
        self.environment["MY_FLOW_CONFIG"] = str(self.root / "config.json")
        Path(self.environment["MY_FLOW_CONFIG"]).write_text(
            json.dumps({"targetWorkspace": str(self.target), "sshHost": "github-test"}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def session(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(SESSION_SCRIPT), *arguments],
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )

    def session_status(self) -> dict[str, object]:
        result = self.session("status", str(self.worktree))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def guard(self, tool_name: str, tool_input: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(GUARD_SCRIPT)],
            input=json.dumps({"tool_name": tool_name, "tool_input": tool_input}),
            capture_output=True,
            check=False,
            env=self.environment,
            text=True,
        )

    def start(self, round_number: int = 1, max_rounds: int | None = None) -> str:
        arguments = ["start", str(self.worktree), "--round", str(round_number)]
        if max_rounds is not None:
            arguments.extend(["--max-rounds", str(max_rounds)])
        result = self.session(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["stopToken"]

    def write_report(self, round_number: int, role: str, findings: list[dict[str, str]]) -> None:
        report_path = self.worktree / ".reviews" / f"round-{round_number:02d}" / f"{role}.json"
        report_path.write_text(
            json.dumps({"role": role, "round": round_number, "findings": findings}), encoding="utf-8"
        )

    def validate_round(self, round_number: int, token: str) -> subprocess.CompletedProcess[str]:
        return self.session(
            "validate", str(self.worktree), "--round", str(round_number), "--token", token
        )

    def stop(self, token: str) -> None:
        result = self.session("stop", str(self.worktree), "--token", token)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_plugin_guard_runs_before_every_tool(self) -> None:
        hooks = json.loads(HOOKS_PATH.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hooks), {"PreToolUse"})
        entries = hooks["PreToolUse"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["matcher"], ".*")
        self.assertEqual(
            entries[0]["hooks"][0]["command"],
            'python3 "${PLUGIN_ROOT}/scripts/review_guard.py"',
        )

    def test_default_round_limit_is_three_and_round_four_is_rejected(self) -> None:
        token = self.start()
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        session = self.session_status()
        self.assertEqual(session["maxRounds"], 3)
        self.assertFalse((self.worktree / ".reviews" / "session.json").exists())
        self.stop(token)
        rejected = self.session("start", str(self.worktree), "--round", "4")
        self.assertNotEqual(rejected.returncode, 0)

    def test_explicit_round_override_is_accepted(self) -> None:
        token = self.start(round_number=1, max_rounds=5)
        session = self.session_status()
        self.assertEqual(session["maxRounds"], 5)
        self.stop(token)

        for role in ("ui", "ux"):
            self.write_report(1, role, [])
        summary = self.worktree / ".reviews" / "round-01" / "summary.json"
        summary.write_text(json.dumps({"round": 1, "unresolved": 0}), encoding="utf-8")
        token = self.start(round_number=2)
        persisted = self.session_status()
        self.assertEqual(persisted["maxRounds"], 5)
        self.stop(token)

    def test_guard_allows_worktree_reads_and_review_report_writes_only(self) -> None:
        token = self.start()
        inside_read = self.guard("Read", {"path": str(self.worktree / "src" / "App.tsx")})
        outside_read = self.guard("Read", {"path": "/etc/passwd"})
        report_patch = "*** Begin Patch\n*** Add File: .reviews/round-01/ui.json\n+{}\n*** End Patch"
        source_patch = "*** Begin Patch\n*** Update File: src/App.tsx\n@@\n-old\n+new\n*** End Patch"
        inside_write = self.guard("apply_patch", report_patch)
        outside_write = self.guard("apply_patch", source_patch)
        self.assertEqual(inside_read.returncode, 0, inside_read.stderr)
        self.assertEqual(inside_write.returncode, 0, inside_write.stderr)
        self.assertEqual(outside_read.returncode, 2)
        self.assertEqual(outside_write.returncode, 2)
        self.stop(token)

    def test_guard_denies_mutating_shell_and_external_sources(self) -> None:
        token = self.start()
        read_only = self.guard("functions.exec_command", {"cmd": "rg App src", "workdir": str(self.worktree)})
        mutating = self.guard("functions.exec_command", {"cmd": "touch src/new.ts", "workdir": str(self.worktree)})
        external = self.guard("web__run", {"search_query": [{"q": "design review"}]})
        connector = self.guard("mcp__github__get_file_contents", {"path": "src/App.tsx"})
        localhost = self.guard("browser_navigate", {"url": "http://localhost:3000", "workdir": str(self.worktree)})
        external_file = self.guard("browser_navigate", {"url": "file:///etc/passwd", "workdir": str(self.worktree)})
        find_exec = self.guard(
            "functions.exec_command",
            {"cmd": "find src -exec touch .reviews/pwned {} +", "workdir": str(self.worktree)},
        )
        self.assertEqual(read_only.returncode, 0, read_only.stderr)
        self.assertEqual(localhost.returncode, 0, localhost.stderr)
        self.assertEqual(mutating.returncode, 2)
        self.assertEqual(external.returncode, 2)
        self.assertEqual(connector.returncode, 2)
        self.assertEqual(external_file.returncode, 2)
        self.assertEqual(find_exec.returncode, 2)
        self.stop(token)

    def test_session_control_cannot_hide_a_trailing_mutation(self) -> None:
        token = self.start()
        command = (
            f"python3 scripts/review_session.py status {self.worktree} "
            "&& touch src/escaped.ts"
        )
        result = self.guard("functions.exec_command", {"cmd": command, "workdir": str(self.worktree)})
        self.assertEqual(result.returncode, 2)
        self.stop(token)

    def test_both_reports_are_required_and_rounds_reduce_unresolved_findings(self) -> None:
        token = self.start()
        self.write_report(1, "ui", [finding("UI-001"), finding("UI-002")])
        missing_ux = self.validate_round(1, token)
        self.assertNotEqual(missing_ux.returncode, 0)
        self.write_report(1, "ux", [finding("UX-001")])
        round_one = self.validate_round(1, token)
        self.assertEqual(round_one.returncode, 0, round_one.stderr)
        self.assertEqual(json.loads(round_one.stdout)["unresolved"], 3)
        self.stop(token)

        token = self.start(round_number=2)
        self.write_report(2, "ui", [finding("UI-001", "completed"), finding("UI-002", "completed")])
        self.write_report(2, "ux", [finding("UX-001")])
        round_two = self.validate_round(2, token)
        self.assertEqual(round_two.returncode, 0, round_two.stderr)
        summary = json.loads(round_two.stdout)
        self.assertEqual(summary["unresolved"], 1)
        self.assertEqual(summary["completed"], 2)
        self.stop(token)

    def test_prior_findings_must_be_retained_and_re_evaluated(self) -> None:
        token = self.start()
        self.write_report(1, "ui", [finding("UI-001")])
        self.write_report(1, "ux", [finding("UX-001")])
        self.assertEqual(self.validate_round(1, token).returncode, 0)
        self.stop(token)

        token = self.start(round_number=2)
        self.write_report(2, "ui", [])
        self.write_report(2, "ux", [finding("UX-001", "completed")])
        result = self.validate_round(2, token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UI-001", result.stderr)
        self.stop(token)

    def test_non_decreasing_round_requires_regression_deferral(self) -> None:
        token = self.start()
        self.write_report(1, "ui", [finding("UI-001")])
        self.write_report(1, "ux", [finding("UX-001")])
        self.assertEqual(self.validate_round(1, token).returncode, 0)
        self.stop(token)

        token = self.start(round_number=2)
        self.write_report(2, "ui", [finding("UI-001")])
        self.write_report(2, "ux", [finding("UX-001")])
        rejected = self.validate_round(2, token)
        self.assertNotEqual(rejected.returncode, 0)
        self.write_report(2, "ui", [finding("UI-001", "deferred-regression", "Changing it would hide required controls.")])
        self.write_report(2, "ux", [finding("UX-001", "deferred-regression", "Changing it would break keyboard navigation.")])
        accepted = self.validate_round(2, token)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(json.loads(accepted.stdout)["progressException"])
        self.stop(token)


if __name__ == "__main__":
    unittest.main()
