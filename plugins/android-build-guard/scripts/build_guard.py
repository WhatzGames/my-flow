#!/usr/bin/env python3
"""Block Android builds that bypass the My Flow copy-on-write wrappers."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any


BASE_VOLUME = "aosp-android-4.4.4-r2.0.1-base"
MANIFEST_REVISION = "android-4.4.4_r2.0.1"
BUILDER_IMAGE = "localhost/aosp-kitkat-wheezy:cow"
STATE_ROOT = Path(os.environ.get("ANDROID_BUILD_GUARD_STATE", Path.home() / ".codex" / "android-build-guard"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BUILD_RE = re.compile(
    r"(?:^|[;&|\n]\s*|\s)(?:"
    r"(?:podman|docker)\s+(?:build|create|run)|"
    r"repo\s+(?:init|sync)|"
    r"(?:make|ninja|m|mm|mmm)(?:\s|$)|"
    r"(?:prepare-aosp-base|build-device|container-build|package-live-usb|make-live-usb)\.sh"
    r")"
)


def main() -> int:
    payload = read_payload()
    tool_name = get_tool_name(payload).lower()
    if not any(name in tool_name for name in ("bash", "shell", "exec_command")):
        return 0

    tool_input = get_tool_input(payload)
    command = first_string(tool_input, ("cmd", "command", "script"))
    workdir_raw = first_string(tool_input, ("workdir", "cwd", "workingDirectory", "working_directory")) or first_string(
        payload, ("cwd", "workdir", "workingDirectory", "working_directory")
    )
    if not command or not workdir_raw:
        return 0

    workdir = Path(workdir_raw).expanduser().resolve()
    if not in_android_scope(workdir, command):
        return 0

    reason = evaluate(command, workdir)
    if reason:
        return block(reason)
    record_pending_build(payload, command, workdir)
    return 0


def evaluate(command: str, workdir: Path) -> str | None:
    argv = split_simple(command)
    if argv and Path(argv[0]).name == "prepare-aosp-base.sh":
        return validate_prepare(argv, workdir)
    if argv and Path(argv[0]).name == "build-device.sh":
        return validate_build(argv, workdir)
    if argv == ["podman", "build", "-t", BUILDER_IMAGE, "."]:
        return validate_builder(workdir)
    if BUILD_RE.search(command):
        return (
            "Android Build Guard blocked a direct build. Use the named "
            "prepare-aosp-base.sh or build-device.sh wrapper from the "
            "android_containerized_build worktree."
        )
    return None


def validate_prepare(argv: list[str], workdir: Path) -> str | None:
    error = validate_builder(workdir)
    if error:
        return error
    if len(argv) != 2 or not NAME_RE.fullmatch(argv[1]):
        return "Base preparation requires one explicit, meaningful container name."
    prepare = workdir / "prepare-aosp-base.sh"
    text = read_text(prepare)
    required = (MANIFEST_REVISION, BASE_VOLUME, "podman run --name")
    if text is None or any(value not in text for value in required) or "--rm" in text:
        return "prepare-aosp-base.sh does not match the pinned named-container r2.0.1 contract."
    return None


def validate_build(argv: list[str], workdir: Path) -> str | None:
    error = validate_builder(workdir)
    if error:
        return error
    if len(argv) != 3 or not NAME_RE.fullmatch(argv[2]):
        return "Device builds require an absolute Device Tree path and an explicit container name."
    device = Path(argv[1]).expanduser()
    if not device.is_absolute() or not device.resolve().name.startswith("android_device_"):
        return "build-device.sh must receive an absolute My Flow android_device_* worktree path."
    env = read_text(device / "build.env")
    if env is None or BASE_VOLUME not in env:
        return f"The Device Tree build.env must select the pinned base volume {BASE_VOLUME}."
    if not (device / "scripts" / "container-build.sh").is_file():
        return "The Device Tree must provide scripts/container-build.sh."
    return None


def validate_builder(workdir: Path) -> str | None:
    if not workdir.name.startswith("android_containerized_build-"):
        return "Run Android build wrappers from an android_containerized_build-* worktree."
    build_script = read_text(workdir / "build-device.sh")
    required = (BASE_VOLUME, f"image=${{BUILDER_IMAGE:-{BUILDER_IMAGE}}}", "/aosp:O", "/kernel:O", "--name")
    if build_script is None or any(value not in build_script for value in required) or "--rm" in build_script:
        return "build-device.sh does not match the pinned COW and named-container contract."
    return None


def record_pending_build(payload: Any, command: str, workdir: Path, state_root: Path = STATE_ROOT) -> None:
    argv = split_simple(command)
    if not argv or Path(argv[0]).name not in {"prepare-aosp-base.sh", "build-device.sh"}:
        return
    session_id = first_string(payload, ("session_id",))
    if not session_id:
        return
    turn_id = first_string(payload, ("turn_id",)) or "turn"
    state_root.mkdir(parents=True, exist_ok=True)
    path = pending_path(state_root, session_id, turn_id)
    state = {
        "container": argv[-1],
        "kind": Path(argv[0]).name,
        "workdir": str(workdir),
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(path)


def pending_path(state_root: Path, session_id: str, turn_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{session_id}--{turn_id}")
    return state_root / f"{safe}.json"


def split_simple(command: str) -> list[str] | None:
    if re.search(r"(?:&&|\|\||[;|<>`\n]|\$\()", command):
        return None
    try:
        return shlex.split(command)
    except ValueError:
        return None


def in_android_scope(workdir: Path, command: str) -> bool:
    return any(
        part.startswith(("android_containerized_build-", "android_device_"))
        for part in workdir.parts
    ) or "android_containerized_build-" in command or "android_device_" in command


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_payload() -> Any:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}


def get_tool_name(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("tool_name", "toolName", "name", "recipient_name"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return ""


def get_tool_input(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in ("tool_input", "toolInput", "input", "parameters"):
            if key in payload:
                return payload[key]
    return payload


def first_string(payload: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(payload, dict):
        for key in keys:
            if isinstance(payload.get(key), str) and payload[key]:
                return payload[key]
    return None


def block(reason: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    print(reason, file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
