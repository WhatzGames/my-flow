#!/usr/bin/env python3
"""Continue the originating Codex turn when its Build Android container exits."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_guard import STATE_ROOT, first_string, pending_path, read_payload


def main() -> int:
    payload = read_payload()
    session_id = first_string(payload, ("session_id",))
    turn_id = first_string(payload, ("turn_id",)) or "turn"
    if not session_id or payload.get("stop_hook_active") is True:
        return no_action()

    path = pending_path(STATE_ROOT, session_id, turn_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return no_action()

    container = state.get("container")
    if not isinstance(container, str) or not container:
        path.unlink(missing_ok=True)
        return no_action()

    reason = wait_for_build(container, path)
    print(json.dumps({"decision": "block", "reason": reason}), flush=True)
    return 0


def wait_for_build(container: str, state_path: Path) -> str:
    try:
        waited = subprocess.run(
            ["podman", "wait", container],
            text=True,
            capture_output=True,
            check=False,
        )
        if waited.returncode != 0:
            return finish_state(
                state_path,
                f"Android build watcher could not wait for `{container}`: {waited.stderr.strip() or 'unknown Podman error'}",
            )
        exit_code = int(waited.stdout.strip().splitlines()[-1])
        if exit_code == 0:
            return finish_state(
                state_path,
                f"Containerized Android build `{container}` completed successfully. Verify the generated artifacts and report the result.",
            )

        logs = subprocess.run(
            ["podman", "logs", "--tail", "200", container],
            text=True,
            capture_output=True,
            check=False,
        )
        combined = "\n".join(part for part in (logs.stdout.strip(), logs.stderr.strip()) if part)
        failure_dir = state_path.parent / "failures"
        failure_dir.mkdir(parents=True, exist_ok=True)
        log_path = failure_dir / f"{container}.log"
        log_path.write_text(combined, encoding="utf-8")
        tail = combined[-12000:] or "No container log output was available."
        return finish_state(
            state_path,
            f"Containerized Android build `{container}` failed with exit code {exit_code}. "
            f"The captured tail is saved at `{log_path}`; full logs remain available through `podman logs {container}`. "
            f"Diagnose the failure and report it to the user.\n\n{tail}",
        )
    except (OSError, ValueError) as error:
        return finish_state(state_path, f"Android build watcher failed for `{container}`: {error}")


def finish_state(path: Path, message: str) -> str:
    path.unlink(missing_ok=True)
    return message


def no_action() -> int:
    print("{}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
