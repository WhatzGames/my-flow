#!/usr/bin/env python3
"""Persist and enforce the My Flow target working directory."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from enforce_ssh_host import repository_remote_violations


CONFIG_ENV = "MY_FLOW_CONFIG"
STARTUP_PATH_ENV = "MY_FLOW_STARTUP_PATH"
SKIP_ENV = "MY_FLOW_SKIP_WORKSPACE_CHECK"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))
MUTATING_TOOL_PATTERNS = (
    "write",
    "edit",
    "apply_patch",
    "multi_edit",
    "exec_command",
    "bash",
    "shell",
)
BARES_DIRECTORY = "bares"
WORKTREES_DIRECTORY = "worktrees"


def main() -> int:
    args = parse_args()
    if args.command == "status":
        return status()
    if args.command == "set":
        return set_target(args.directory)
    if args.command == "set-host":
        return set_ssh_host(args.ssh_host)
    if args.command == "startup":
        return startup()
    if args.command == "clear":
        return clear_target()
    if args.command == "refresh":
        return refresh_bare_repositories()
    if args.command == "hook":
        return hook()
    raise AssertionError(args.command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage the My Flow target workspace.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Print current target workspace as JSON.")
    set_parser = subparsers.add_parser("set", help="Persist the target workspace.")
    set_parser.add_argument("directory")
    host_parser = subparsers.add_parser("set-host", help="Persist the only allowed SSH Host alias.")
    host_parser.add_argument("ssh_host")
    subparsers.add_parser("startup", help="Initialize the target workspace from config or startup context.")
    subparsers.add_parser("clear", help="Forget the target workspace.")
    subparsers.add_parser("refresh", help="Fetch all managed bare repositories.")
    subparsers.add_parser("hook", help="Run as a Codex hook.")
    return parser.parse_args()


def status() -> int:
    target = load_target()
    ssh_host = load_ssh_host()
    print(json.dumps(workspace_status(target, ssh_host)))
    return 0 if target and ssh_host else 1


def set_target(directory: str) -> int:
    target = Path(directory).expanduser().resolve()
    if not target.is_dir():
        print(f"My Flow target does not exist or is not a directory: {target}", file=sys.stderr)
        return 1
    bares, worktrees = ensure_layout(target)
    write_config(target, load_ssh_host())
    print(json.dumps(workspace_status(target, load_ssh_host(), bares, worktrees)))
    return 0


def set_ssh_host(ssh_host: str) -> int:
    target = load_target()
    if target is None:
        print("Set the My Flow target workspace before selecting an SSH Host.", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", ssh_host):
        print("SSH Host aliases may contain only letters, numbers, dots, underscores, and hyphens.", file=sys.stderr)
        return 1
    write_config(target, ssh_host)
    print(json.dumps(workspace_status(target, ssh_host)))
    return 0


def startup() -> int:
    payload = read_payload()
    target = load_target()
    created = False
    if target is None:
        target = startup_target(payload)
        if target is None:
            print(json.dumps({"targetWorkspace": None, "sshHost": load_ssh_host(), "bares": None, "worktrees": None}))
            return 0
        target.mkdir(parents=True, exist_ok=True)
        created = True

    bares, worktrees = ensure_layout(target)
    ssh_host = load_ssh_host()
    write_config(target, ssh_host)
    status_payload = workspace_status(target, ssh_host, bares, worktrees)
    status_payload["created"] = created
    print(json.dumps(status_payload))
    return 0


def startup_target(payload: Any) -> Path | None:
    configured = os.environ.get(STARTUP_PATH_ENV)
    if configured:
        return Path(configured).expanduser().resolve()

    for candidate in startup_path_candidates(payload):
        path = Path(candidate).expanduser().resolve()
        if is_my_flow_internal_path(path):
            continue
        return path
    return None


def startup_path_candidates(payload: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(payload, dict):
        for key in ("startupPath", "startup_path", "cwd", "workdir", "workingDirectory", "working_directory"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        for key in ("tool_input", "toolInput", "input", "parameters"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.extend(startup_path_candidates(value))
    return candidates


def write_config(target: Path, ssh_host: str | None) -> None:
    payload: dict[str, str] = {"targetWorkspace": str(target)}
    if ssh_host:
        payload["sshHost"] = ssh_host
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def clear_target() -> int:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    print(json.dumps({"targetWorkspace": None, "sshHost": None}))
    return 0


def refresh_bare_repositories() -> int:
    target = load_target()
    ssh_host = load_ssh_host()
    if target is None or ssh_host is None:
        print(json.dumps({"targetWorkspace": str(target) if target else None, "sshHost": ssh_host, "fetched": [], "failed": []}))
        return 0

    bares, _ = ensure_layout(target)
    repositories = sorted(path for path in bares.iterdir() if path.is_dir() and is_bare_repository(path))
    fetched: list[str] = []
    failed: list[dict[str, str]] = []
    for repository in repositories:
        remote_violations = repository_remote_violations(repository, ssh_host, bare=True)
        if remote_violations:
            failed.append({"repository": repository.name, "error": remote_violations[0]})
            continue
        configuration_error = configure_missing_fetch_specs(repository)
        if configuration_error is not None:
            failed.append({"repository": repository.name, "error": configuration_error})
            continue
        result = subprocess.run(
            ["git", f"--git-dir={repository}", "fetch", "--all", "--prune"],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0:
            fetched.append(repository.name)
        else:
            detail = result.stderr.strip() or result.stdout.strip() or f"git exited {result.returncode}"
            failed.append({"repository": repository.name, "error": detail})

    print(json.dumps({"targetWorkspace": str(target), "sshHost": ssh_host, "fetched": fetched, "failed": failed}))
    if failed:
        print("My Flow could not refresh every bare repository.", file=sys.stderr)
        return 1
    return 0


def ensure_layout(target: Path) -> tuple[Path, Path]:
    bares = target / BARES_DIRECTORY
    worktrees = target / WORKTREES_DIRECTORY
    bares.mkdir(parents=True, exist_ok=True)
    worktrees.mkdir(parents=True, exist_ok=True)
    return bares, worktrees


def workspace_status(
    target: Path | None,
    ssh_host: str | None,
    bares: Path | None = None,
    worktrees: Path | None = None,
) -> dict[str, str | None]:
    if target is None:
        return {"targetWorkspace": None, "sshHost": ssh_host, "bares": None, "worktrees": None}
    return {
        "targetWorkspace": str(target),
        "sshHost": ssh_host,
        "bares": str(bares or target / BARES_DIRECTORY),
        "worktrees": str(worktrees or target / WORKTREES_DIRECTORY),
    }


def is_bare_repository(path: Path) -> bool:
    result = subprocess.run(
        ["git", f"--git-dir={path}", "rev-parse", "--is-bare-repository"],
        capture_output=True,
        check=False,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def configure_missing_fetch_specs(repository: Path) -> str | None:
    remotes = subprocess.run(
        ["git", f"--git-dir={repository}", "remote"],
        capture_output=True,
        check=False,
        text=True,
    )
    if remotes.returncode != 0:
        return remotes.stderr.strip() or "could not list Git remotes"

    for remote in filter(None, (line.strip() for line in remotes.stdout.splitlines())):
        key = f"remote.{remote}.fetch"
        current = subprocess.run(
            ["git", f"--git-dir={repository}", "config", "--get-all", key],
            capture_output=True,
            check=False,
            text=True,
        )
        if current.returncode == 0 and current.stdout.strip():
            continue
        fetch_spec = f"+refs/heads/*:refs/remotes/{remote}/*"
        configured = subprocess.run(
            ["git", f"--git-dir={repository}", "config", "--add", key, fetch_spec],
            capture_output=True,
            check=False,
            text=True,
        )
        if configured.returncode != 0:
            return configured.stderr.strip() or f"could not configure fetch refspec for remote {remote}"
    return None


def hook() -> int:
    if os.environ.get(SKIP_ENV) in {"1", "true", "yes"}:
        return 0
    payload = read_payload()
    if is_target_workspace_command(payload):
        return 0

    target = load_target()
    if target is None:
        return block(
            "My Flow needs a target working directory before work starts. "
            "Ask the user which directory to use, then persist it with "
            "`python3 scripts/target_workspace.py set /absolute/path` from the my-flow plugin root."
        )

    if load_ssh_host() is None:
        return block(
            "My Flow needs the SSH Host alias from the user's SSH config. "
            "Ask which `Host` entry to use, then persist it once with "
            "`python3 scripts/target_workspace.py set-host HOST_ALIAS` from the my-flow plugin root."
        )

    ensure_layout(target)
    violation = find_workspace_violation(payload, target)
    if violation is not None:
        return block(violation)
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
    except (OSError, json.JSONDecodeError):
        return None
    raw_target = payload.get("targetWorkspace")
    if not isinstance(raw_target, str) or not raw_target:
        return None
    target = Path(raw_target).expanduser().resolve()
    return target if target.is_dir() else None


def load_ssh_host() -> str | None:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("sshHost")
    return value if isinstance(value, str) and value else None


def block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    print(reason, file=sys.stderr, flush=True)
    return 2


def is_target_workspace_command(payload: Any) -> bool:
    for text in extract_text(payload):
        if "target_workspace.py" in text and re.search(r"\b(set|set-host|startup|status|clear|refresh)\b", text):
            return True
    return False


def find_workspace_violation(payload: Any, target: Path) -> str | None:
    tool_name = get_tool_name(payload)
    lower_tool = tool_name.lower()
    if not any(pattern in lower_tool for pattern in MUTATING_TOOL_PATTERNS):
        return None

    worktrees = target / WORKTREES_DIRECTORY
    input_payload = get_tool_input(payload)
    workdir = first_string(input_payload, ("workdir", "cwd", "workingDirectory", "working_directory"))
    command = first_string(input_payload, ("cmd", "command", "script"))

    if "apply_patch" in lower_tool and command:
        for path in patch_paths(command):
            if not is_within(path, worktrees) and not is_my_flow_internal_path(path):
                return f"My Flow blocked a patch outside managed worktrees directory `{worktrees}`: `{path}`."

    if command and looks_like_shell_tool(lower_tool):
        command_violation = check_shell_command(command, workdir, worktrees)
        if command_violation is not None:
            return command_violation

    for path in absolute_paths(input_payload):
        if not is_within(path, worktrees) and not is_my_flow_internal_path(path):
            return f"My Flow blocked implementation work outside `{worktrees}`. Path `{path}` is not in the managed worktrees directory."

    if workdir:
        resolved_workdir = Path(workdir).expanduser().resolve()
        if not is_within(resolved_workdir, worktrees) and not is_my_flow_internal_path(resolved_workdir):
            return f"My Flow blocked workdir `{resolved_workdir}` outside managed worktrees directory `{worktrees}`."

    return None


def patch_paths(command: str) -> list[Path]:
    paths: list[Path] = []
    for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.MULTILINE):
        raw_path = match.group(1).strip()
        if raw_path.startswith("/"):
            paths.append(Path(raw_path).expanduser().resolve())
    return paths


def check_shell_command(command: str, workdir: str | None, worktrees: Path) -> str | None:
    if workdir is None:
        return f"My Flow requires shell commands to set workdir inside `{worktrees}`."
    resolved_workdir = Path(workdir).expanduser().resolve()
    if not is_within(resolved_workdir, worktrees) and not is_my_flow_internal_path(resolved_workdir):
        return f"My Flow blocked shell workdir `{resolved_workdir}` outside managed worktrees directory `{worktrees}`."

    for argv in shell_words(command):
        if argv[:1] == ["cd"] and len(argv) >= 2:
            cd_target = Path(argv[1]).expanduser()
            if not cd_target.is_absolute():
                cd_target = resolved_workdir / cd_target
            cd_target = cd_target.resolve()
            if not is_within(cd_target, worktrees):
                return f"My Flow blocked `cd {argv[1]}` because it leaves `{worktrees}`."
    return None


def shell_words(text: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for part in re.split(r"\s*(?:&&|\|\||;|\n)\s*", text):
        if not part.strip():
            continue
        try:
            commands.append(shlex.split(part))
        except ValueError:
            commands.append(part.split())
    return commands


def absolute_paths(value: Any) -> list[Path]:
    paths: list[Path] = []
    if isinstance(value, str) and value.startswith("/"):
        paths.append(Path(value).expanduser().resolve())
    elif isinstance(value, list):
        for item in value:
            paths.extend(absolute_paths(item))
    elif isinstance(value, dict):
        for item in value.values():
            paths.extend(absolute_paths(item))
    return paths


def extract_text(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        texts.append(value)
    elif isinstance(value, list):
        for item in value:
            texts.extend(extract_text(item))
    elif isinstance(value, dict):
        for item in value.values():
            texts.extend(extract_text(item))
    return texts


def first_string(payload: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


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


def looks_like_shell_tool(tool_name: str) -> bool:
    return "bash" in tool_name or "shell" in tool_name or "exec_command" in tool_name


def is_within(path: Path, target: Path) -> bool:
    return path == target or target in path.parents


def is_my_flow_internal_path(path: Path) -> bool:
    plugin_root = Path(__file__).resolve().parents[1]
    return path == plugin_root or plugin_root in path.parents or path == CONFIG_PATH or CONFIG_PATH in path.parents


if __name__ == "__main__":
    raise SystemExit(main())
