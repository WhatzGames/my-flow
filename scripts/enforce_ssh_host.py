#!/usr/bin/env python3
"""Restrict SSH and network Git transports to one persisted SSH host alias."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from require_github_push_approval import command_argv, extract_text, shell_words


CONFIG_ENV = "MY_FLOW_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))
GIT_NETWORK_COMMANDS = {"clone", "fetch", "pull", "push", "ls-remote", "submodule"}
REMOTE_ARGUMENT_PATTERN = re.compile(r"^(?:[^@/:\s]+@)?([^/:\s]+):.+$")


def main() -> int:
    payload = read_payload()
    ssh_host = load_ssh_host()
    if ssh_host is None:
        return 0

    violation = find_violation(payload, ssh_host)
    if violation is None:
        return 0
    reason = f"My Flow allows SSH and network Git access only through SSH Host `{ssh_host}`. {violation}"
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    print(reason, file=sys.stderr, flush=True)
    return 2


def read_payload() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def load_ssh_host() -> str | None:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("sshHost")
    return value if isinstance(value, str) and value else None


def find_violation(payload: Any, ssh_host: str) -> str | None:
    workdir = find_workdir(payload)
    for text in extract_text(payload):
        for argv in shell_words(text):
            environment_violation = environment_override_violation(argv)
            if environment_violation:
                return environment_violation
            argv = command_argv(argv)
            if not argv:
                continue
            executable = Path(argv[0]).name
            if executable == "ssh":
                override = ssh_override_violation(argv[1:])
                if override:
                    return override
                destination = ssh_destination(argv[1:])
                if destination and destination != ssh_host:
                    return f"Command targets SSH host `{destination}`."
            elif executable in {"scp", "sftp", "rsync"}:
                override = ssh_override_violation(argv[1:], rsync=executable == "rsync")
                if override:
                    return override
                for argument in argv[1:]:
                    host = remote_argument_host(argument)
                    if host and host != ssh_host:
                        return f"Command targets SSH host `{host}`."
            elif executable == "git":
                reason = validate_git_argv(argv, workdir, ssh_host)
                if reason:
                    return reason
            elif executable == "gh" and argv[1:3] == ["repo", "clone"]:
                return "`gh repo clone` cannot guarantee the configured SSH alias; use `git clone <ssh-host>:owner/repo.git`."
    return None


def validate_git_argv(argv: list[str], workdir: Path | None, ssh_host: str) -> str | None:
    for argument in argv:
        normalized = argument.lower()
        if "core.sshcommand=" in normalized or (normalized.startswith("url.") and ".insteadof=" in normalized):
            return "Per-command Git SSH or URL rewrites are not allowed."
    command = git_subcommand(argv)
    for argument in argv:
        violation = validate_remote_url(argument, ssh_host)
        if violation:
            return violation
    if command not in GIT_NETWORK_COMMANDS:
        return None

    repository = git_repository_path(argv, workdir)
    if repository is None:
        return None
    violations = repository_remote_violations(repository, ssh_host)
    return violations[0] if violations else None


def validate_remote_url(value: str, ssh_host: str) -> str | None:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https", "git"} and parsed.hostname:
        return f"Network Git URL `{value}` does not use the configured SSH Host."
    if parsed.scheme == "ssh" and parsed.hostname:
        if parsed.hostname != ssh_host:
            return f"SSH Git URL targets `{parsed.hostname}` instead of `{ssh_host}`."
        return None
    host = remote_argument_host(value)
    if host and host != ssh_host:
        return f"SSH Git URL targets `{host}` instead of `{ssh_host}`."
    return None


def repository_remote_violations(repository: Path, ssh_host: str, bare: bool = False) -> list[str]:
    prefix = ["git", f"--git-dir={repository}"] if bare else ["git", "-C", str(repository)]
    remotes = subprocess.run([*prefix, "remote"], capture_output=True, check=False, text=True)
    if remotes.returncode != 0:
        return []

    violations: list[str] = []
    for remote in filter(None, (line.strip() for line in remotes.stdout.splitlines())):
        urls: set[str] = set()
        for mode in ([], ["--push"]):
            result = subprocess.run(
                [*prefix, "remote", "get-url", *mode, "--all", remote],
                capture_output=True,
                check=False,
                text=True,
            )
            if result.returncode == 0:
                urls.update(line.strip() for line in result.stdout.splitlines() if line.strip())
        for url in sorted(urls):
            violation = validate_remote_url(url, ssh_host)
            if violation:
                violations.append(f"Remote `{remote}` is not allowed: {violation}")
    return violations


def git_subcommand(argv: list[str]) -> str | None:
    skip_next = False
    for index, argument in enumerate(argv[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if argument in {"-C", "-c", "--git-dir", "--work-tree"}:
            skip_next = True
            continue
        if argument.startswith(("--git-dir=", "--work-tree=")) or argument.startswith("-"):
            continue
        return argument if index < len(argv) else None
    return None


def git_repository_path(argv: list[str], workdir: Path | None) -> Path | None:
    for index, argument in enumerate(argv[:-1]):
        if argument == "-C":
            return Path(argv[index + 1]).expanduser().resolve()
    return workdir


def ssh_destination(arguments: list[str]) -> str | None:
    options_with_values = {"-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w"}
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument in options_with_values:
            skip_next = True
            continue
        if argument.startswith("-"):
            continue
        return argument.rsplit("@", 1)[-1]
    return None


def environment_override_violation(argv: list[str]) -> str | None:
    for argument in argv:
        if re.match(r"^(?:GIT_SSH|GIT_SSH_COMMAND|GIT_CONFIG_(?:COUNT|KEY_\d+|VALUE_\d+))=", argument):
            return "Per-command Git SSH or config environment overrides are not allowed."
        if argument == "env":
            continue
        if "=" not in argument:
            break
    return None


def ssh_override_violation(arguments: list[str], rsync: bool = False) -> str | None:
    if rsync and any(argument in {"-e", "--rsh"} or argument.startswith("--rsh=") for argument in arguments):
        return "A custom rsync SSH command is not allowed."
    for index, argument in enumerate(arguments):
        if argument in {"-F", "-J", "-W"}:
            return f"SSH option `{argument}` can bypass the configured Host and is not allowed."
        option = None
        if argument == "-o" and index + 1 < len(arguments):
            option = arguments[index + 1]
        elif argument.startswith("-o") and len(argument) > 2:
            option = argument[2:]
        if option and option.split("=", 1)[0].lower() in {"hostname", "proxycommand", "proxyjump"}:
            return f"SSH option `{option.split('=', 1)[0]}` can bypass the configured Host and is not allowed."
    return None


def remote_argument_host(value: str) -> str | None:
    if value.startswith(("/", "./", "../", "file://")):
        return None
    if ":" in value:
        left, right = value.split(":", 1)
        if left in {"HEAD", "FETCH_HEAD", "ORIG_HEAD"} or left.startswith("refs/") or right.startswith("refs/"):
            return None
    match = REMOTE_ARGUMENT_PATTERN.match(value)
    return match.group(1) if match else None


def find_workdir(payload: Any) -> Path | None:
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


if __name__ == "__main__":
    raise SystemExit(main())
