from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import harness.storage.locks as locks
from harness.core.errors import HarnessError
from harness.runtime import validate_runtime_platform
from harness.sandbox_exec import _run_windows_python_child
from harness.services.process_control import process_start_identity
from harness.storage.locks import FileLock
from harness.storage.safe_open import open_regular_readonly
from harness.write_sandbox import (
    _without_windows_extended_prefix,
    require_write_sandbox,
)

ROOT = Path(__file__).resolve().parents[2]


class PortableSafeOpenTests(unittest.TestCase):
    def test_windows_sandbox_runs_managed_python_script_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "managed_agent.py"
            output = root / "result.json"
            script.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "Path(sys.argv[1]).write_text(json.dumps({"
                "'argv': sys.argv[2:], 'file': __file__}))\n",
                encoding="utf-8",
            )
            previous_argv = sys.argv
            try:
                result = _run_windows_python_child(
                    [
                        sys.executable,
                        str(script.resolve()),
                        str(output),
                        "--host",
                        "127.0.0.1",
                    ]
                )
            finally:
                sys.argv = previous_argv

            self.assertEqual(result, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["argv"], ["--host", "127.0.0.1"])
            self.assertEqual(Path(payload["file"]), script.resolve())

    def test_extended_windows_paths_share_the_declared_root_namespace(self) -> None:
        self.assertEqual(
            _without_windows_extended_prefix(r"\\?\D:\workspace\projects\deck"),
            r"D:\workspace\projects\deck",
        )
        self.assertEqual(
            _without_windows_extended_prefix(
                r"\\?\UNC\server\share\workspace\projects\deck"
            ),
            r"\\server\share\workspace\projects\deck",
        )
        self.assertEqual(
            _without_windows_extended_prefix(r"D:\workspace\projects\deck"),
            r"D:\workspace\projects\deck",
        )
        self.assertEqual(
            _without_windows_extended_prefix(r"\\?\GLOBALROOT\Device\Harddisk0"),
            r"\\?\GLOBALROOT\Device\Harddisk0",
        )

    def test_managed_python_audit_allows_only_declared_write_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            blocked = root / "blocked"
            allowed.mkdir()
            blocked.mkdir()
            code = (
                "from pathlib import Path; "
                "from harness.write_sandbox import _apply_windows_write_sandbox; "
                f"allowed=Path({str(allowed)!r}); blocked=Path({str(blocked)!r}); "
                "_apply_windows_write_sandbox([allowed]); "
                "(allowed/'result.txt').write_text('ok'); "
                "\ntry: (blocked/'result.txt').write_text('bad'); escaped=True\n"
                "except PermissionError: escaped=False\n"
                "print('escaped=' + json.dumps(escaped))"
            )
            result = subprocess.run(
                [sys.executable, "-c", "import json; " + code],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.stdout.strip(), "escaped=false")
            self.assertEqual((allowed / "result.txt").read_text(), "ok")
            self.assertFalse((blocked / "result.txt").exists())

    def test_resolved_nested_asset_stays_beneath_an_equivalent_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "instances" / "image" / "outputs"
            nested.mkdir(parents=True)
            asset = nested / "preview.png"
            asset.write_bytes(b"private-image")

            descriptor = open_regular_readonly(
                asset.resolve(strict=True), trusted_root=root
            )
            os.close(descriptor)

    def test_windows_lock_retries_when_another_thread_initializes_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = FileLock(Path(temporary) / "writer.lock", 0.1)
            busy = PermissionError(
                errno.EACCES, "the marker byte is already locked"
            )
            with (
                patch.object(locks.os, "name", "nt"),
                patch.object(locks.os, "open", return_value=42),
                patch.object(locks.os, "close"),
                patch.object(
                    locks.os, "fstat", return_value=SimpleNamespace(st_size=0)
                ),
                patch.object(locks.os, "write", side_effect=[busy, 1]) as write,
                patch.object(FileLock, "_lock") as native_lock,
                patch.object(locks.time, "sleep"),
            ):
                lock.acquire()

            self.assertTrue(lock.acquired)
            self.assertEqual(write.call_count, 2)
            native_lock.assert_called_once_with(42)
            lock._descriptor = None


@unittest.skipUnless(os.name == "nt", "requires a real Windows kernel")
class WindowsRuntimeTests(unittest.TestCase):
    def test_sandbox_exec_runs_script_entrypoint_with_active_write_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            allowed = root / "allowed"
            blocked = root / "blocked"
            allowed.mkdir()
            blocked.mkdir()
            script = root / "managed_agent.py"
            result_path = allowed / "result.json"
            blocked_path = blocked / "escaped.txt"
            script.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "blocked = Path(sys.argv[2])\n"
                "try:\n"
                "    blocked.write_text('bad')\n"
                "    escaped = True\n"
                "except PermissionError:\n"
                "    escaped = False\n"
                "Path(sys.argv[1]).write_text(json.dumps({"
                "'escaped': escaped, 'argv': sys.argv[3:]}))\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "backend" / "harness" / "sandbox_exec.py"),
                    json.dumps([str(allowed.resolve())]),
                    sys.executable,
                    str(script.resolve()),
                    str(result_path),
                    str(blocked_path),
                    "--port",
                    "19300",
                ],
                check=True,
            )

            self.assertEqual(completed.returncode, 0)
            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8")),
                {"escaped": False, "argv": ["--port", "19300"]},
            )
            self.assertFalse(blocked_path.exists())

    def test_managed_write_sandbox_is_available(self) -> None:
        require_write_sandbox()

    def test_executable_path_and_handle_metadata_share_one_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "runtime-launcher.exe"
            executable.write_bytes(b"not-executed")

            descriptor = open_regular_readonly(executable, trusted_root=root)
            os.close(descriptor)

    def test_native_runtime_preflight_and_process_identity(self) -> None:
        validate_runtime_platform()
        self.assertIsNotNone(process_start_identity(os.getpid()))

    def test_real_windows_file_lock_serializes_writers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "writer.lock"
            with FileLock(path, 0.1), self.assertRaises(HarnessError) as blocked:
                FileLock(path, 0.03).acquire()
            self.assertEqual(blocked.exception.code, "REVISION_CONFLICT")


if __name__ == "__main__":
    unittest.main()
