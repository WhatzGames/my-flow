#!/usr/bin/env python3
"""Scaffold and validate workspace boundaries for My Flow marketplace plugins."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PLUGIN_ROOT / "templates" / "workspace_guard.py"
MARKETPLACE_RELATIVE_PATH = Path(".agents/plugins/marketplace.json")
WORKSPACE_HOOK_COMMAND = "./scripts/workspace_guard.py"
MY_FLOW_HOOK_COMMAND = "./scripts/target_workspace.py hook"
DEFAULT_CREATOR = Path.home() / ".codex" / "skills" / ".system" / "plugin-creator" / "scripts" / "create_basic_plugin.py"


def main() -> int:
    args = parse_args()
    try:
        if args.command == "validate":
            root = resolve_marketplace_root(args.root)
            validate_marketplace(root)
            print(json.dumps({"marketplace": str(root), "valid": True}))
            return 0
        if args.command == "create":
            root = resolve_marketplace_root(args.root)
            plugin = create_plugin(root, args.name, args.category, Path(args.creator_script).expanduser().resolve())
            print(json.dumps({"marketplace": str(root), "plugin": str(plugin), "valid": True}))
            return 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"My Flow marketplace boundary validation failed: {error}", file=sys.stderr)
        return 1
    raise AssertionError(args.command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage enforced workspace boundaries in a My Flow marketplace.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="Validate every marketplace plugin boundary.")
    validate.add_argument("--root", help="Marketplace repository root; auto-detected when omitted.")
    create = subparsers.add_parser("create", help="Create a guarded plugin and append it to the marketplace.")
    create.add_argument("name")
    create.add_argument("--root", help="Marketplace repository root; auto-detected when omitted.")
    create.add_argument("--category", default="Developer Tools")
    create.add_argument("--creator-script", default=str(DEFAULT_CREATOR))
    return parser.parse_args()


def resolve_marketplace_root(raw_root: str | None) -> Path:
    if raw_root:
        root = Path(raw_root).expanduser().resolve()
        if not (root / MARKETPLACE_RELATIVE_PATH).is_file():
            raise ValueError(f"marketplace manifest not found under {root}")
        return root
    for start in (Path.cwd().resolve(), PLUGIN_ROOT):
        for candidate in (start, *start.parents):
            if (candidate / MARKETPLACE_RELATIVE_PATH).is_file():
                return candidate
    raise ValueError("could not find .agents/plugins/marketplace.json")


def validate_marketplace(root: Path) -> None:
    marketplace_path = root / MARKETPLACE_RELATIVE_PATH
    payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
    entries = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{marketplace_path} must contain a non-empty plugins array")

    failures: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            failures.append("marketplace contains a non-object plugin entry")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            failures.append("marketplace plugin entry has no valid name")
            continue
        if name in seen:
            failures.append(f"{name}: duplicate marketplace entry")
            continue
        seen.add(name)
        source = entry.get("source")
        raw_path = source.get("path") if isinstance(source, dict) and source.get("source") == "local" else None
        if not isinstance(raw_path, str):
            failures.append(f"{name}: source must be a local path")
            continue
        plugin_root = (root / raw_path).resolve()
        if root != plugin_root and root not in plugin_root.parents:
            failures.append(f"{name}: source escapes marketplace root: {plugin_root}")
            continue
        failures.extend(validate_plugin(name, plugin_root))
    if failures:
        raise RuntimeError("; ".join(failures))


def validate_plugin(name: str, plugin_root: Path) -> list[str]:
    failures: list[str] = []
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return [f"{name}: invalid or missing plugin manifest ({error})"]
    if manifest.get("name") != name:
        failures.append(f"{name}: plugin manifest name does not match marketplace entry")

    command = MY_FLOW_HOOK_COMMAND if name == "my-flow" else WORKSPACE_HOOK_COMMAND
    try:
        hooks = json.loads((plugin_root / "hooks.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return failures + [f"{name}: invalid or missing hooks.json ({error})"]
    if not has_universal_pretool_hook(hooks, command):
        failures.append(f"{name}: missing universal PreToolUse workspace hook `{command}`")

    guard = plugin_root / ("scripts/target_workspace.py" if name == "my-flow" else "scripts/workspace_guard.py")
    if not guard.is_file():
        failures.append(f"{name}: workspace guard script is missing: {guard}")
    elif not os.access(guard, os.X_OK):
        failures.append(f"{name}: workspace guard script is not executable: {guard}")
    elif name != "my-flow" and digest(guard) != digest(TEMPLATE):
        failures.append(f"{name}: workspace guard differs from the canonical My Flow template")
    return failures


def has_universal_pretool_hook(payload: Any, command: str) -> bool:
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("matcher") != ".*":
            continue
        commands = entry.get("hooks")
        if isinstance(commands, list) and any(
            isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == command
            for hook in commands
        ):
            return True
    return False


def create_plugin(root: Path, raw_name: str, category: str, creator: Path) -> Path:
    name = normalize_name(raw_name)
    if not name or len(name) > 64:
        raise ValueError("plugin name must normalize to 1-64 lowercase hyphenated characters")
    if not creator.is_file():
        raise ValueError(f"plugin-creator scaffold script not found: {creator}")
    destination = root / "plugins" / name
    if destination.exists():
        raise ValueError(f"plugin already exists: {destination}")

    command = [
        sys.executable,
        str(creator),
        name,
        "--path",
        str(root / "plugins"),
        "--marketplace-path",
        str(root / MARKETPLACE_RELATIVE_PATH),
        "--with-marketplace",
        "--with-skills",
        "--with-scripts",
        "--category",
        category,
    ]
    result = subprocess.run(command, capture_output=True, check=False, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"creator exited {result.returncode}"
        raise RuntimeError(detail)

    guard = destination / "scripts" / "workspace_guard.py"
    shutil.copyfile(TEMPLATE, guard)
    guard.chmod(0o755)
    hooks = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [{"type": "command", "command": WORKSPACE_HOOK_COMMAND}],
                }
            ]
        }
    }
    (destination / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    validate_marketplace(root)
    return destination


def normalize_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    return re.sub(r"-{2,}", "-", normalized).strip("-")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
