#!/usr/bin/env python3
"""Block GitHub publishing unless Codex is using an approval path.

The hook is intentionally stateless. It does not grant approval itself; it only
prevents silent pushes and tells the agent to request explicit user approval.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from typing import Any


APPROVAL_ENV_VARS = (
    "CODEX_USER_APPROVED",
    "CODEX_APPROVED",
    "CODEX_SANDBOX_ESCALATED",
    "MY_FLOW_GITHUB_PUSH_APPROVED",
)

GITHUB_API_WRITE_HINTS = (
    "/git/refs",
    "/git/tags",
    "/git/commits",
    "/git/trees",
    "/pulls",
    "/releases",
)

GH_WRITE_SUBCOMMANDS = {
    ("pr", "create"),
    ("pr", "merge"),
    ("pr", "ready"),
    ("release", "create"),
    ("release", "upload"),
    ("repo", "sync"),
    ("workflow", "run"),
}


def main() -> int:
    payload = read_payload()
    texts = list(extract_text(payload))

    reason = classify_publish_attempt(texts)
    if reason is None:
        return 0

    if has_approval_signal(payload):
        return 0

    message = (
        "My Flow blocked a GitHub publishing action: "
        f"{reason}. Ask the user for explicit approval before pushing, "
        "publishing, merging, creating releases, or writing GitHub refs."
    )
    print(json.dumps({"decision": "block", "reason": message}), flush=True)
    print(message, file=sys.stderr, flush=True)
    return 2


def read_payload() -> Any:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


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


def classify_publish_attempt(texts: list[str]) -> str | None:
    for text in texts:
        if is_git_push(text):
            return "git push"
        gh_reason = classify_gh_write(text)
        if gh_reason is not None:
            return gh_reason
        connector_reason = classify_github_connector_write(text)
        if connector_reason is not None:
            return connector_reason
    return None


def is_git_push(text: str) -> bool:
    for argv in shell_words(text):
        argv = command_argv(argv)
        if not argv:
            continue
        if argv[0] != "git":
            continue
        if "push" in argv[1:]:
            return True
    return bool(re.search(r"(^|[;&|]\s*)git\s+[^;&|]*\bpush\b", text))


def classify_gh_write(text: str) -> str | None:
    for argv in shell_words(text):
        argv = command_argv(argv)
        if not argv or argv[0] != "gh":
            continue
        command_words = [word for word in argv[1:] if not word.startswith("-")]
        if len(command_words) >= 2 and tuple(command_words[:2]) in GH_WRITE_SUBCOMMANDS:
            return "gh " + " ".join(command_words[:2])
        if command_words[:1] == ["api"] and is_mutating_gh_api(argv):
            return "gh api write"
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


def command_argv(argv: list[str]) -> list[str]:
    index = 0
    if argv[:1] == ["env"]:
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            index += 1
    while index < len(argv) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", argv[index], re.DOTALL):
        index += 1
    return argv[index:]


def is_mutating_gh_api(argv: list[str]) -> bool:
    joined = " ".join(argv)
    uses_write_method = bool(re.search(r"(^|\s)(-X|--method)\s+(POST|PUT|PATCH|DELETE)\b", joined))
    writes_ref_like_endpoint = any(hint in joined for hint in GITHUB_API_WRITE_HINTS)
    return uses_write_method and writes_ref_like_endpoint


def classify_github_connector_write(text: str) -> str | None:
    normalized = text.lower().replace("-", "_")
    if "github" not in normalized and not normalized.startswith("gh_"):
        return None
    publish_terms = (
        "push",
        "create_ref",
        "update_ref",
        "delete_ref",
        "merge_pull",
        "merge_pr",
        "create_release",
        "upload_release",
        "workflow_run",
    )
    if any(term in normalized for term in publish_terms):
        return "GitHub connector publishing call"
    return None


def has_approval_signal(payload: Any) -> bool:
    if any(os.environ.get(name) in {"1", "true", "yes", "approved"} for name in APPROVAL_ENV_VARS):
        return True
    payload_text = json.dumps(payload, sort_keys=True).lower()
    return (
        '"sandbox_permissions": "require_escalated"' in payload_text
        or '"sandbox_permissions":"require_escalated"' in payload_text
        or '"approved": true' in payload_text
        or '"approval": "approved"' in payload_text
    )


if __name__ == "__main__":
    raise SystemExit(main())
