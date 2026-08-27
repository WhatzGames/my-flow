#!/usr/bin/env python3
"""Enforce isolated, patch-only Android source builds."""

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
STATE_ROOT = Path(os.environ.get("BUILD_ANDROID_STATE", Path.home() / ".codex" / "build-android"))
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
BUILD_RE = re.compile(
    r"(?:^|[;&|\n]\s*|\s)(?:"
    r"(?:podman|docker)\s+(?:build|create|run)|"
    r"repo\s+(?:init|sync)|"
    r"(?:make|ninja|m|mm|mmm)(?:\s|$)|"
    r"(?:prepare-aosp-base|build-device|container-build|package-live-usb|make-live-usb)\.sh"
    r")"
)
SHELL_MUTATION_RE = re.compile(
    r"(?:^|[;&|\n]\s*|\s)(?:"
    r"sed\s+-i|perl\s+-pi|(?:python\d*|ruby)\s+-c|"
    r"(?:cp|mv|rm|install|truncate|touch|tee|dd)\b|"
    r"git\s+(?:apply|checkout|reset|clean)|patch\b|"
    r"(?:bash|sh)\s+-c"
    r")",
    re.IGNORECASE,
)
INLINE_SOURCE_EDIT_RE = re.compile(
    r"(?im)^\s*(?:sed\s+-i|perl\s+-pi|(?:python\d*|ruby)\s+-c|"
    r"(?:cp|mv|rm|install|truncate|touch|tee|dd)\b|(?:bash|sh)\s+-c)"
    r"[^\n]*(?:/aosp(?:/|\b)|/kernel(?:/|\b)|\$tree\b)"
)
SOURCE_REDIRECT_RE = re.compile(r"(?m)(?:>|>>)\s*(?:/aosp|/kernel)(?:/|\b)")
REDIRECT_TARGET_RE = re.compile(r"(?:^|\s)(?:>|>>)\s*([^\s;&|]+)")
PATCH_PATH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
PATCH_MOVE_RE = re.compile(r"^\*\*\* Move to: (.+)$", re.MULTILINE)
READ_ONLY_CONTAINER_COMMANDS = {"ps", "logs", "inspect", "wait"}


def main() -> int:
    payload = read_payload()
    tool_name = get_tool_name(payload).lower()
    tool_input = get_tool_input(payload)
    workdir_raw = first_string(tool_input, ("workdir", "cwd", "workingDirectory", "working_directory")) or first_string(
        payload, ("cwd", "workdir", "workingDirectory", "working_directory")
    )
    if not workdir_raw:
        return 0
    workdir = Path(workdir_raw).expanduser().resolve()

    if any(name in tool_name for name in ("bash", "shell", "exec_command")):
        command = first_string(tool_input, ("cmd", "command", "script"))
        if not command or not in_android_scope(workdir, command):
            return 0
        reason = evaluate(command, workdir)
        if not reason:
            record_pending_build(payload, command, workdir)
    elif tool_name == "apply_patch" or any(name in tool_name for name in ("write", "edit")):
        reason = evaluate_write(tool_name, tool_input, workdir)
    else:
        return 0

    if reason:
        return block(reason)
    return 0


def evaluate(command: str, workdir: Path) -> str | None:
    argv = split_simple(command)
    if argv and Path(argv[0]).name == "prepare-aosp-base.sh":
        return validate_prepare(argv, workdir)
    if argv and Path(argv[0]).name == "build-device.sh":
        return validate_build(argv, workdir)
    if argv == ["podman", "build", "-t", BUILDER_IMAGE, "."]:
        return validate_builder(workdir)
    if argv and Path(argv[0]).name in {"podman", "docker"}:
        if len(argv) > 1 and argv[1] in READ_ONLY_CONTAINER_COMMANDS:
            return None
        return (
            "Build Android blocked a direct container mutation. Use build-device.sh; "
            "AOSP and kernel changes must be committed as .patch files in the Device Tree."
        )
    if BUILD_RE.search(command):
        return (
            "Build Android blocked a direct build. Use the named "
            "prepare-aosp-base.sh or build-device.sh wrapper from the "
            "android_containerized_build worktree."
        )
    if redirects_to_protected_source(command, workdir) or (
        SHELL_MUTATION_RE.search(command) and (source_tree_root(workdir) or mentions_container_source(command))
    ):
        return (
            "Build Android blocked a direct AOSP/kernel edit. Create a .patch file in the "
            "Device Tree patches directory and let build-device.sh apply it to the COW overlay."
        )
    return None


def evaluate_write(tool_name: str, tool_input: Any, workdir: Path) -> str | None:
    if tool_name == "apply_patch":
        command = first_string(tool_input, ("command", "patch", "input"))
        paths = extract_patch_paths(command or "")
    else:
        paths = extract_structured_paths(tool_input)

    if source_tree_root(workdir) and not paths:
        return patch_only_reason()
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        resolved = path.resolve() if path.is_absolute() else (workdir / path).resolve()
        if is_protected_source_path(raw_path, resolved):
            return patch_only_reason()
    return None


def patch_only_reason() -> str:
    return (
        "Build Android blocked a direct AOSP/kernel file edit. Store the change as a .patch "
        "file under an android_device_* or android_containerized_build-* patches directory."
    )


def extract_patch_paths(command: str) -> list[str]:
    return PATCH_PATH_RE.findall(command) + PATCH_MOVE_RE.findall(command)


def extract_structured_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = key.lower().replace("-", "_")
            if normalized in {"path", "file", "file_path", "filename", "target", "destination"} and isinstance(item, str):
                paths.append(item)
            else:
                paths.extend(extract_structured_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(extract_structured_paths(item))
    return paths


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
    container_script = read_text(device / "scripts" / "container-build.sh")
    if container_script is None:
        return "The Device Tree must provide scripts/container-build.sh."
    patch_error = validate_patch_contract(container_script)
    if patch_error:
        return patch_error
    return None


def validate_patch_contract(script: str) -> str | None:
    if INLINE_SOURCE_EDIT_RE.search(script) or SOURCE_REDIRECT_RE.search(script):
        return "container-build.sh contains an inline AOSP/kernel edit; move it into a Device Tree .patch file."

    for line in script.splitlines():
        stripped = line.strip()
        if not stripped.startswith("apply_patch_once "):
            if re.match(r"^(?:git\s+apply|patch\b).*(?:/aosp|/kernel)", stripped):
                return "container-build.sh may modify AOSP/kernel only through apply_patch_once."
            continue
        try:
            argv = shlex.split(stripped)
        except ValueError:
            return "container-build.sh contains an invalid apply_patch_once command."
        if len(argv) != 3 or argv[1] not in {"/aosp", "/kernel"}:
            return "AOSP/kernel patches must use apply_patch_once with an explicit source tree."
        patch_path = argv[2]
        if not patch_path.startswith(("/device/patches/", "/workspace/patches/")):
            return "AOSP/kernel patches must come from a repository patches directory."
        if not (patch_path.endswith(".patch") or patch_path.endswith("$patch_name")):
            return "AOSP/kernel adjustments must be stored in .patch files."
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
    ) or source_tree_root(workdir) is not None or mentions_container_source(command) or \
        "android_containerized_build-" in command or "android_device_" in command


def mentions_container_source(text: str) -> bool:
    return bool(re.search(r"(?:^|\s)(?:/aosp|/kernel)(?:/|\b)", text)) or any(
        marker in text for marker in (BASE_VOLUME, "kernel-3.19-src")
    )


def redirects_to_protected_source(command: str, workdir: Path) -> bool:
    for raw_path in REDIRECT_TARGET_RE.findall(command):
        path = Path(raw_path.strip("'\"")).expanduser()
        resolved = path.resolve() if path.is_absolute() else (workdir / path).resolve()
        if is_protected_source_path(str(path), resolved):
            return True
    return False


def is_protected_source_path(raw_path: str, resolved: Path) -> bool:
    normalized = raw_path.replace("\\", "/")
    return normalized == "/aosp" or normalized.startswith("/aosp/") or \
        normalized == "/kernel" or normalized.startswith("/kernel/") or \
        source_tree_root(resolved) is not None


def source_tree_root(path: Path) -> Path | None:
    current = path if path.is_dir() else path.parent
    for candidate in (current, *current.parents):
        if (candidate / ".repo").is_dir() and (candidate / "build" / "envsetup.sh").is_file():
            return candidate
        if (candidate / "Kconfig").is_file() and (candidate / "arch").is_dir() and (candidate / "drivers").is_dir():
            return candidate
    return None


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
