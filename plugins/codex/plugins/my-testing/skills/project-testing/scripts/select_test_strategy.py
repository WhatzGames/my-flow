#!/usr/bin/env python3
"""Print a project-aware testing strategy for a repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


def run_git(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def remote_slug(url: str) -> tuple[str, str]:
    if not url:
        return "", ""

    patterns = [
        r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"git@[^:]+:(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
        r"ssh://git@[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group("owner"), match.group("repo")
    return "", ""


def has_file(root: Path, *names: str) -> bool:
    return any((root / name).exists() for name in names)


def read_sample(root: Path) -> str:
    candidates = [
        "README.md",
        "CONTRIBUTING.md",
        "Cargo.toml",
        "package.json",
        "pyproject.toml",
        "go.mod",
        "justfile",
        "Makefile",
        "scripts/build.sh",
        "scripts/test.sh",
        "scripts/publish.sh",
    ]
    parts: list[str] = []
    for name in candidates:
        path = root / name
        if path.is_file():
            try:
                parts.append(path.read_text(errors="ignore")[:12000])
            except OSError:
                pass
    return "\n".join(parts).lower()


def package_scripts(root: Path) -> list[str]:
    path = root / "package.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    scripts = data.get("scripts", {})
    if not isinstance(scripts, dict):
        return []
    return sorted(str(name) for name in scripts)


def detect(root: Path) -> dict[str, object]:
    root = root.resolve()
    remote = run_git(root, "remote", "get-url", "origin")
    owner, repo = remote_slug(remote)
    owners = {
        value.strip()
        for value in os.environ.get("MY_TESTING_OWNERS", "").split(",")
        if value.strip()
    }

    sample = read_sample(root)
    freenet_indicators = [
        "freenet",
        "fdev",
        "web contract",
        "scripts/publish.sh",
        "pj_web_bg.wasm",
        "bridge.js",
    ]
    is_freenet = any(indicator in sample for indicator in freenet_indicators)
    is_own_repo = owner in owners

    stacks: list[str] = []
    if has_file(root, "Cargo.toml"):
        stacks.append("rust")
    if has_file(root, "package.json"):
        stacks.append("node")
    if has_file(root, "pyproject.toml", "setup.py", "requirements.txt"):
        stacks.append("python")
    if list(root.glob("*.sln")) or list(root.glob("*.csproj")):
        stacks.append("dotnet")
    if has_file(root, "go.mod"):
        stacks.append("go")

    scripts = package_scripts(root)

    if is_freenet and is_own_repo:
        strategy = "freenet-own-repo"
        commands = [
            "./scripts/build.sh",
            "python3 -m http.server 4173 --directory dist --bind 127.0.0.1",
            "./scripts/publish.sh",
        ]
        notes = [
            "Verify the static site first, then smoke-test the local Freenet wrapper URL.",
            "Use explicit desktop and mobile viewports; assert no horizontal overflow.",
            "If the Freenet URL is blank but the static site works, debug shell/iframe/CSP/bridge/assets before CSS.",
        ]
    elif "rust" in stacks:
        strategy = "rust"
        commands = ["cargo fmt --check", "cargo test"]
        notes = ["Add clippy when the repository convention includes it."]
    elif "node" in stacks:
        strategy = "node"
        commands = []
        for script in ["test", "lint", "typecheck", "check", "build"]:
            if script in scripts:
                commands.append(f"npm run {script}" if script != "test" else "npm test")
        if not commands:
            commands.append("npm test")
        notes = ["Prefer the package manager already used by the repo."]
    elif "python" in stacks:
        strategy = "python"
        commands = ["pytest"]
        notes = ["Add ruff, mypy, or coverage only when configured or requested."]
    elif "go" in stacks:
        strategy = "go"
        commands = ["go test ./..."]
        notes = []
    elif "dotnet" in stacks:
        strategy = "dotnet"
        commands = ["dotnet test"]
        notes = []
    else:
        strategy = "inspect-docs"
        commands = []
        notes = ["No common stack marker found; inspect README, Makefile, justfile, and scripts/."]

    return {
        "root": str(root),
        "remote": remote,
        "owner": owner,
        "repo": repo,
        "own_repo": is_own_repo,
        "freenet": is_freenet,
        "stacks": stacks,
        "package_scripts": scripts,
        "strategy": strategy,
        "commands": commands,
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="Repository root")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()

    result = detect(Path(args.root))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    print(f"root: {result['root']}")
    if result["remote"]:
        print(f"remote: {result['remote']}")
    if result["owner"] or result["repo"]:
        print(f"repository: {result['owner']}/{result['repo']}")
    print(f"strategy: {result['strategy']}")
    if result["stacks"]:
        print("stacks: " + ", ".join(result["stacks"]))
    if result["commands"]:
        print("commands:")
        for command in result["commands"]:
            print(f"  - {command}")
    if result["notes"]:
        print("notes:")
        for note in result["notes"]:
            print(f"  - {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
