from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.runtime_identity import PythonInterpreterIdentity

from scripts.dev import (
    DevelopmentLauncher,
    LauncherError,
    _console_safe,
    content_tree_digest,
    input_digest,
    main,
    venv_python,
)


class DevelopmentLauncherTests(unittest.TestCase):
    def test_child_output_is_safe_for_legacy_windows_console_encoding(self) -> None:
        message = "[frontend] ➜ ready"

        self.assertEqual(_console_safe(message, encoding="utf-8"), message)
        self.assertEqual(
            _console_safe(message, encoding="cp1252"),
            "[frontend] \\u279c ready",
        )

    def test_virtual_environment_interpreter_is_cross_platform(self) -> None:
        root = Path("environment")

        self.assertEqual(venv_python(root, os_name="posix"), root / "bin" / "python")
        self.assertEqual(
            venv_python(root, os_name="nt"), root / "Scripts" / "python.exe"
        )

    def test_input_digest_binds_relative_names_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "one" / "lock.txt"
            second = root / "two" / "lock.txt"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text("same", encoding="utf-8")
            second.write_text("same", encoding="utf-8")

            original = input_digest([first, second], root=root)
            second.write_text("changed", encoding="utf-8")

            self.assertNotEqual(original, input_digest([first, second], root=root))

    def test_dependency_digest_ignores_install_metadata_but_detects_code_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "example" / "module.py"
            package.parent.mkdir()
            package.write_text("VALUE = 1\n", encoding="utf-8")
            metadata = root / "example-1.0.dist-info"
            metadata.mkdir()
            (metadata / "RECORD").write_text("location-specific", encoding="utf-8")
            stamp = root / ".requirements-installed"
            stamp.write_text("first", encoding="utf-8")

            original = content_tree_digest(root)
            stamp.write_text("second", encoding="utf-8")
            (metadata / "RECORD").write_text("changed", encoding="utf-8")
            self.assertEqual(original, content_tree_digest(root))

            package.write_text("VALUE = 2\n", encoding="utf-8")
            self.assertNotEqual(original, content_tree_digest(root))

    def test_lock_embedded_path_must_be_directly_below_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock_path = root / "agents" / "image-agent.lock.json"
            lock_path.parent.mkdir()
            lock_path.write_text(
                json.dumps({"embedded_path": "agents/image_agent_mvp"}),
                encoding="utf-8",
            )
            launcher = DevelopmentLauncher(root=root)
            self.assertEqual(
                launcher.image_agent_root, root / "agents" / "image_agent_mvp"
            )

            lock_path.write_text(
                json.dumps({"embedded_path": "elsewhere/image_agent_mvp"}),
                encoding="utf-8",
            )
            with self.assertRaises(LauncherError):
                _ = launcher.image_agent_root

    def test_frontend_lock_change_invalidates_node_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frontend = root / "frontend"
            node_modules = frontend / "node_modules"
            node_modules.mkdir(parents=True)
            lock_path = frontend / "package-lock.json"
            lock_path.write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
            launcher = DevelopmentLauncher(root=root)
            stamp = node_modules / ".harness-package-lock.sha256"
            stamp.write_text(launcher.frontend_input_digest() + "\n", encoding="utf-8")

            with (
                patch("scripts.dev.shutil.which", return_value="npm"),
                patch("scripts.dev.command_succeeds", return_value=True),
            ):
                self.assertTrue(launcher.frontend_is_current())
                lock_path.write_text('{"lockfileVersion": 4}\n', encoding="utf-8")
                self.assertFalse(launcher.frontend_is_current())

    def test_venv_without_ensurepip_uses_clean_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = DevelopmentLauncher(root=root)

            def create_environment(command, **_kwargs):
                if "--without-pip" not in command:
                    launcher.venv_root.mkdir()
                    raise subprocess.CalledProcessError(1, command)
                launcher.venv_python.parent.mkdir(parents=True)
                launcher.venv_python.write_text("placeholder", encoding="utf-8")

            def interpreter_identity(command):
                executable = str(Path(command[0]).absolute())
                return PythonInterpreterIdentity(
                    implementation="cpython",
                    cache_tag="cpython-313",
                    version="3.13.7",
                    executable=executable,
                    is_virtual_environment=Path(command[0]) == launcher.venv_python,
                )

            with (
                patch("scripts.dev.run_command", side_effect=create_environment),
                patch("scripts.dev.command_succeeds", return_value=True),
                patch.object(
                    DevelopmentLauncher,
                    "interpreter_identity",
                    side_effect=interpreter_identity,
                ),
            ):
                launcher.ensure_virtual_environment()

            self.assertTrue(launcher.venv_python.is_file())
            self.assertEqual(list(root.glob(".venv-failed-*")), [])

    def test_image_input_digest_binds_the_runtime_and_actual_pip_interpreters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = root / "agents" / "image-agent.lock.json"
            lock.parent.mkdir()
            lock.write_text(
                json.dumps(
                    {
                        "embedded_path": "agents/image_agent_mvp",
                        "dependencies": {"files": []},
                    }
                ),
                encoding="utf-8",
            )
            runtime = PythonInterpreterIdentity(
                "cpython", "cpython-313", "3.13.7", "/runtime/python", True
            )
            first_installer = PythonInterpreterIdentity(
                "cpython", "cpython-313", "3.13.7", "/pip/python", True
            )
            second_installer = PythonInterpreterIdentity(
                "cpython", "cpython-314", "3.14.0", "/other/python", False
            )
            launcher = DevelopmentLauncher(root=root)

            self.assertNotEqual(
                launcher.image_input_digest(runtime, first_installer),
                launcher.image_input_digest(runtime, second_installer),
            )

    def test_doctor_can_continue_with_an_actionable_image_degradation(self) -> None:
        launcher = DevelopmentLauncher(root=Path("workspace"))
        with (
            patch.object(DevelopmentLauncher, "config_check"),
            patch.object(DevelopmentLauncher, "check_tools"),
            patch.object(DevelopmentLauncher, "verify_image_lock"),
            patch.object(DevelopmentLauncher, "backend_is_current", return_value=True),
            patch.object(
                DevelopmentLauncher,
                "require_current_image_dependencies",
                side_effect=LauncherError("run scripts/dev.py setup --force"),
            ),
            patch.object(DevelopmentLauncher, "frontend_is_current", return_value=True),
            patch.object(DevelopmentLauncher, "_check_configuration"),
            patch.object(DevelopmentLauncher, "_check_writable_runtime"),
        ):
            launcher.doctor(check_ports=False, allow_image_degraded=True)

    def test_busy_port_is_reported_before_process_start(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            port = int(occupied.getsockname()[1])
            with self.assertRaisesRegex(LauncherError, str(port)):
                DevelopmentLauncher._check_port("127.0.0.1", port)

    def test_cli_rejects_shared_backend_and_frontend_port(self) -> None:
        self.assertEqual(
            main(
                [
                    "doctor",
                    "--backend-port",
                    "19000",
                    "--frontend-port",
                    "19000",
                ]
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
