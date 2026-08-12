#!/usr/bin/env python3
"""Validate the required Codex co-author trailer on Git commits."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path


TRAILER = "Co-authored-by: GPT-5 <noreply@openai.com>"
ZERO_SHA = "0" * 40
MARKETPLACE_MANIFEST = Path(".agents/plugins/marketplace.json")
BOUNDARY_VALIDATOR = Path(__file__).resolve().parents[1] / "scripts" / "marketplace_boundaries.py"


def git(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], capture_output=True, check=False, text=True)


def commits_for_push(remote_name: str, updates: Iterable[str]) -> list[str]:
    commits: set[str] = set()
    for update in updates:
        fields = update.split()
        if len(fields) != 4:
            continue
        _local_ref, local_sha, _remote_ref, remote_sha = fields
        if local_sha == ZERO_SHA:
            continue
        if remote_sha == ZERO_SHA:
            result = git("rev-list", local_sha, "--not", f"--remotes={remote_name}")
        else:
            result = git("rev-list", f"{remote_sha}..{local_sha}")
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "could not enumerate outgoing commits"
            raise RuntimeError(detail)
        commits.update(filter(None, result.stdout.splitlines()))
    return sorted(commits)


def rewritten_commits(lines: Iterable[str]) -> list[str]:
    commits: set[str] = set()
    for line in lines:
        fields = line.split()
        if len(fields) >= 2:
            commits.add(fields[1])
    return sorted(commits)


def validate_marketplace_boundaries() -> None:
    root_result = git("rev-parse", "--show-toplevel")
    if root_result.returncode != 0:
        return
    root = Path(root_result.stdout.strip()).resolve()
    if not (root / MARKETPLACE_MANIFEST).is_file():
        return
    if not BOUNDARY_VALIDATOR.is_file():
        raise RuntimeError(f"marketplace boundary validator is missing: {BOUNDARY_VALIDATOR}")
    result = subprocess.run(
        [sys.executable, str(BOUNDARY_VALIDATOR), "validate", "--root", str(root)],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "marketplace boundary validation failed"
        raise RuntimeError(detail)


def missing_trailer(commits: Iterable[str]) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for commit in commits:
        result = git("show", "-s", "--format=%B%x00%s", commit)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"could not inspect {commit}"
            raise RuntimeError(detail)
        message, _, subject = result.stdout.partition("\x00")
        if TRAILER not in message:
            missing.append((commit, subject.strip()))
    return missing


def report_missing(commits: list[tuple[str, str]], context: str) -> int:
    if not commits:
        return 0
    print(f"My Flow detected rewritten/outgoing commits without the required co-author trailer during {context}:", file=sys.stderr)
    for commit, subject in commits:
        print(f"  {commit[:12]} {subject}", file=sys.stderr)
    print(f"Required trailer: {TRAILER}", file=sys.stderr)
    print("Repair the commit messages before publishing the rewritten history.", file=sys.stderr)
    return 1
