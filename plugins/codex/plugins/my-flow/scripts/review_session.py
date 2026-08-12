#!/usr/bin/env python3
"""Manage guarded frontend-review sessions and validate concise findings."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any


CONFIG_ENV = "MY_FLOW_CONFIG"
CONFIG_PATH = Path(os.environ.get(CONFIG_ENV, Path.home() / ".codex" / "my-flow" / "config.json"))
SESSION_DIRECTORY = CONFIG_PATH.parent / "review-sessions"
REVIEWS_DIRECTORY = ".reviews"
ROLES = ("ui", "ux")
STATUSES = {"open", "completed", "deferred-regression"}
SEVERITIES = {"high", "medium", "low"}


def main() -> int:
    args = parse_args()
    try:
        worktree = managed_worktree(args.worktree)
        if args.command == "start":
            return start(worktree, args.round, args.max_rounds)
        if args.command == "validate":
            return validate(worktree, args.round, args.token)
        if args.command == "stop":
            return stop(worktree, args.token)
        if args.command == "status":
            return status(worktree)
    except ReviewError as error:
        print(str(error), file=sys.stderr)
        return 1
    raise AssertionError(args.command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage a My Flow frontend-review session.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("worktree")
    start_parser.add_argument("--round", type=int, required=True)
    start_parser.add_argument("--max-rounds", type=int)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("worktree")
    validate_parser.add_argument("--round", type=int, required=True)
    validate_parser.add_argument("--token", required=True)
    stop_parser = subparsers.add_parser("stop")
    stop_parser.add_argument("worktree")
    stop_parser.add_argument("--token", required=True)
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("worktree")
    return parser.parse_args()


def managed_worktree(raw_worktree: str) -> Path:
    target = load_target()
    if target is None:
        raise ReviewError("My Flow target workspace is not configured.")
    worktrees = (target / "worktrees").resolve()
    worktree = Path(raw_worktree).expanduser().resolve()
    if worktree.parent != worktrees or not worktree.is_dir():
        raise ReviewError(f"Review target must be an immediate managed worktree in {worktrees}: {worktree}")
    return worktree


def start(worktree: Path, round_number: int, max_rounds: int | None) -> int:
    session_path = review_session_path(worktree)
    existing = read_json(session_path, required=False)
    if max_rounds is None:
        previous_maximum = existing.get("maxRounds") if isinstance(existing, dict) else None
        max_rounds = previous_maximum if isinstance(previous_maximum, int) else 3
    if round_number < 1 or max_rounds < 1 or round_number > max_rounds:
        raise ReviewError("Round must be between 1 and max-rounds.")
    active_elsewhere = active_review_worktrees(worktree.parent, exclude=worktree)
    if active_elsewhere:
        raise ReviewError(f"Another guarded review is already active: {active_elsewhere[0]}")
    reviews = worktree / REVIEWS_DIRECTORY
    if isinstance(existing, dict) and existing.get("active") is True:
        raise ReviewError(f"A review session is already active for {worktree}.")
    if round_number > 1 and not (reviews / f"round-{round_number - 1:02d}" / "summary.json").is_file():
        raise ReviewError("Validate the preceding round before starting the next round.")

    token = secrets.token_urlsafe(32)
    reviews.mkdir(parents=True, exist_ok=True)
    (reviews / f"round-{round_number:02d}").mkdir(parents=True, exist_ok=True)
    SESSION_DIRECTORY.mkdir(parents=True, exist_ok=True)
    write_json(session_path, {
        "active": True,
        "worktree": str(worktree),
        "round": round_number,
        "maxRounds": max_rounds,
        "stopTokenHash": token_hash(token),
    })
    print(json.dumps({"session": str(session_path), "round": round_number, "maxRounds": max_rounds, "stopToken": token}))
    return 0


def validate(worktree: Path, round_number: int, token: str) -> int:
    session = require_active_session(worktree)
    require_token(session, token)
    if session.get("round") != round_number:
        raise ReviewError(f"Active session is for round {session.get('round')}, not round {round_number}.")

    reports = {role: validate_report(worktree, role, round_number) for role in ROLES}
    previous = previous_findings(worktree, round_number)
    current_findings = [finding for report in reports.values() for finding in report["findings"]]
    current_by_id = {finding["id"]: finding for finding in current_findings}
    missing = sorted(set(previous) - set(current_by_id))
    if missing:
        raise ReviewError("Prior findings must be re-evaluated and retained with a current status: " + ", ".join(missing))

    unresolved = [finding for finding in current_findings if finding["status"] != "completed"]
    previous_unresolved = [finding for finding in previous.values() if finding["status"] != "completed"]
    progress_exception = False
    if round_number > 1 and len(unresolved) >= len(previous_unresolved):
        progress_exception = bool(unresolved) and all(
            finding["status"] == "deferred-regression" and finding.get("regression_risk", "").strip()
            for finding in unresolved
        )
        if not progress_exception:
            raise ReviewError(
                f"Round {round_number} has {len(unresolved)} unresolved findings; it must be below "
                f"round {round_number - 1}'s {len(previous_unresolved)}, unless every remaining item is deferred with a regression risk."
            )

    summary = {
        "round": round_number,
        "open": sum(finding["status"] == "open" for finding in current_findings),
        "completed": sum(finding["status"] == "completed" for finding in current_findings),
        "deferredRegression": sum(finding["status"] == "deferred-regression" for finding in current_findings),
        "unresolved": len(unresolved),
        "previousUnresolved": len(previous_unresolved) if round_number > 1 else None,
        "progressException": progress_exception,
    }
    summary_path = worktree / REVIEWS_DIRECTORY / f"round-{round_number:02d}" / "summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary))
    return 0


def validate_report(worktree: Path, role: str, round_number: int) -> dict[str, Any]:
    path = worktree / REVIEWS_DIRECTORY / f"round-{round_number:02d}" / f"{role}.json"
    report = read_json(path)
    if not isinstance(report, dict) or report.get("role") != role or report.get("round") != round_number:
        raise ReviewError(f"{path} must declare role={role!r} and round={round_number}.")
    findings = report.get("findings")
    if not isinstance(findings, list):
        raise ReviewError(f"{path} must contain a findings array.")
    seen: set[str] = set()
    prefix = role.upper() + "-"
    for finding in findings:
        if not isinstance(finding, dict):
            raise ReviewError(f"Every finding in {path} must be an object.")
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not finding_id.startswith(prefix) or finding_id in seen:
            raise ReviewError(f"Every {role} finding needs a unique {prefix} identifier.")
        seen.add(finding_id)
        if finding.get("severity") not in SEVERITIES or finding.get("status") not in STATUSES:
            raise ReviewError(f"{finding_id} has an invalid severity or status.")
        for field in ("summary", "evidence", "recommendation"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise ReviewError(f"{finding_id} requires concise {field} text.")
        if len(finding["summary"]) > 160:
            raise ReviewError(f"{finding_id} summary exceeds 160 characters.")
        if finding["status"] == "deferred-regression" and not str(finding.get("regression_risk", "")).strip():
            raise ReviewError(f"{finding_id} needs regression_risk when deferred.")
    return report


def previous_findings(worktree: Path, round_number: int) -> dict[str, dict[str, Any]]:
    if round_number <= 1:
        return {}
    findings: dict[str, dict[str, Any]] = {}
    for role in ROLES:
        report = read_json(worktree / REVIEWS_DIRECTORY / f"round-{round_number - 1:02d}" / f"{role}.json")
        if not isinstance(report, dict) or not isinstance(report.get("findings"), list):
            raise ReviewError("The preceding round reports are incomplete.")
        for finding in report["findings"]:
            if isinstance(finding, dict) and isinstance(finding.get("id"), str):
                findings[finding["id"]] = finding
    return findings


def stop(worktree: Path, token: str) -> int:
    session_path = review_session_path(worktree)
    session = require_active_session(worktree)
    require_token(session, token)
    session["active"] = False
    session.pop("stopTokenHash", None)
    write_json(session_path, session)
    print(json.dumps({"session": str(session_path), "active": False}))
    return 0


def status(worktree: Path) -> int:
    session = read_json(review_session_path(worktree), required=False)
    print(json.dumps(session or {"active": False, "worktree": str(worktree)}))
    return 0


def require_active_session(worktree: Path) -> dict[str, Any]:
    session = read_json(review_session_path(worktree))
    if not isinstance(session, dict) or session.get("active") is not True or session.get("worktree") != str(worktree):
        raise ReviewError(f"No active review session for {worktree}.")
    return session


def load_target() -> Path | None:
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        target = payload.get("targetWorkspace")
    except (OSError, json.JSONDecodeError):
        return None
    return Path(target).expanduser().resolve() if isinstance(target, str) and target else None


def active_review_worktrees(worktrees: Path, exclude: Path | None = None) -> list[Path]:
    active: list[Path] = []
    if not worktrees.is_dir() or not SESSION_DIRECTORY.is_dir():
        return active
    for path in SESSION_DIRECTORY.glob("*.json"):
        session = read_json(path, required=False)
        raw_worktree = session.get("worktree") if isinstance(session, dict) else None
        if not isinstance(raw_worktree, str):
            continue
        worktree = Path(raw_worktree).expanduser().resolve()
        if worktree.parent != worktrees.resolve():
            continue
        if exclude is not None and worktree.resolve() == exclude.resolve():
            continue
        if isinstance(session, dict) and session.get("active") is True and session.get("worktree") == str(worktree.resolve()):
            active.append(worktree.resolve())
    return sorted(active)


def review_session_path(worktree: Path) -> Path:
    digest = hashlib.sha256(str(worktree.resolve()).encode("utf-8")).hexdigest()
    return SESSION_DIRECTORY / f"{digest}.json"


def read_json(path: Path, required: bool = True) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        if not required:
            return None
        raise ReviewError(f"Could not read valid JSON from {path}: {error}") from error


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_token(session: dict[str, Any], token: str) -> None:
    if not secrets.compare_digest(str(session.get("stopTokenHash", "")), token_hash(token)):
        raise ReviewError("Invalid review coordinator token.")


class ReviewError(RuntimeError):
    pass


if __name__ == "__main__":
    raise SystemExit(main())
