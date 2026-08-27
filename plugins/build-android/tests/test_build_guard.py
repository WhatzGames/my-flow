from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_guard import BASE_VOLUME, evaluate, evaluate_write, pending_path, record_pending_build
from scripts.build_completion import wait_for_build

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class BuildGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.builder = root / "android_containerized_build-main"
        self.device = root / "android_device_lenovo_miix320-main"
        (self.device / "scripts").mkdir(parents=True)
        self.builder.mkdir()
        (self.builder / "build-device.sh").write_text(
            f"aosp_base={BASE_VOLUME}\n"
            "image=${BUILDER_IMAGE:-localhost/aosp-kitkat-wheezy:cow}\n"
            "podman run --name x -v $aosp_base:/aosp:O -v kernel:/kernel:O\n",
            encoding="utf-8",
        )
        (self.builder / "prepare-aosp-base.sh").write_text(
            f"volume={BASE_VOLUME}\nandroid-4.4.4_r2.0.1\npodman run --name x\n",
            encoding="utf-8",
        )
        (self.device / "build.env").write_text(f"AOSP_BASE_VOLUME={BASE_VOLUME}\n", encoding="utf-8")
        (self.device / "scripts" / "container-build.sh").write_text(
            "apply_patch_once /aosp /device/patches/0001-aosp.patch\n"
            "apply_patch_once /kernel /device/patches/0002-kernel.patch\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_only_wrapped_build_is_allowed(self):
        command = f"./build-device.sh {self.device} miix320-build-001"
        self.assertIsNone(evaluate(command, self.builder))
        self.assertIsNotNone(evaluate("make -j8 systemimage", self.device))
        self.assertIsNotNone(evaluate("podman run image make", self.builder))
        self.assertIsNotNone(evaluate("podman exec miix320-build-001 sed -i s/a/b/ /aosp/build/core/main.mk", self.builder))
        self.assertIsNone(evaluate("podman logs miix320-build-001", self.builder))

    def test_aosp_and_kernel_edits_must_be_repository_patches(self):
        aosp = Path(self.temp.name) / "aosp"
        (aosp / ".repo").mkdir(parents=True)
        (aosp / "build").mkdir()
        (aosp / "build" / "envsetup.sh").touch()
        direct_edit = {
            "command": f"*** Begin Patch\n*** Update File: {aosp}/build/core/main.mk\n@@\n-old\n+new\n*** End Patch"
        }
        self.assertIsNotNone(evaluate_write("apply_patch", direct_edit, self.device))
        self.assertIsNotNone(evaluate("printf changed > build/core/main.mk", aosp))

        patch_edit = {
            "command": f"*** Begin Patch\n*** Add File: {self.device}/patches/0014-fix.patch\n+diff\n*** End Patch"
        }
        self.assertIsNone(evaluate_write("apply_patch", patch_edit, self.device))

        (self.device / "scripts" / "container-build.sh").write_text(
            "sed -i 's/old/new/' /aosp/build/core/main.mk\n",
            encoding="utf-8",
        )
        command = f"./build-device.sh {self.device} miix320-build-002"
        self.assertIsNotNone(evaluate(command, self.builder))

    def test_hook_covers_shell_and_file_edits(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        pre_tool = hooks["PreToolUse"][0]
        self.assertIn("Bash", pre_tool["matcher"])
        self.assertIn("apply_patch", pre_tool["matcher"])
        self.assertIn("${PLUGIN_ROOT}", pre_tool["hooks"][0]["command"])

    def test_build_is_recorded_and_failure_continues_with_logs(self):
        state_root = Path(self.temp.name) / "state"
        command = f"./build-device.sh {self.device} miix320-build-001"
        payload = {"session_id": "session", "turn_id": "turn"}
        record_pending_build(payload, command, self.builder, state_root)
        state = pending_path(state_root, "session", "turn")
        self.assertEqual(json.loads(state.read_text())["container"], "miix320-build-001")

        completed = subprocess_result(0, "2\n", "")
        logs = subprocess_result(0, "compiler failed\n", "")
        with patch("scripts.build_completion.subprocess.run", side_effect=[completed, logs]):
            message = wait_for_build("miix320-build-001", state)

        self.assertIn("exit code 2", message)
        self.assertIn("compiler failed", message)
        self.assertFalse(state.exists())


def subprocess_result(returncode, stdout, stderr):
    from subprocess import CompletedProcess

    return CompletedProcess([], returncode, stdout, stderr)


if __name__ == "__main__":
    unittest.main()
