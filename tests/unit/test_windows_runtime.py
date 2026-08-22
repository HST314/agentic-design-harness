from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import HarnessError
from harness.runtime import validate_runtime_platform
from harness.services.process_control import process_start_identity
from harness.storage.locks import FileLock
from harness.storage.safe_open import open_regular_readonly


@unittest.skipUnless(os.name == "nt", "requires a real Windows kernel")
class WindowsRuntimeTests(unittest.TestCase):
    def test_executable_path_and_handle_metadata_share_one_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "runtime-launcher.exe"
            executable.write_bytes(b"not-executed")

            descriptor = open_regular_readonly(executable, trusted_root=root)
            os.close(descriptor)

    def test_nested_private_asset_and_handle_share_one_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "instances" / "image" / "outputs"
            nested.mkdir(parents=True)
            asset = nested / "preview.png"
            asset.write_bytes(b"private-image")

            descriptor = open_regular_readonly(asset, trusted_root=root)
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
