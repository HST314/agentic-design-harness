from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters.image_lock import (
    default_image_agent_lock_path,
    load_image_agent_lock,
    runtime_platform_key,
)
from harness.core.errors import HarnessError

from scripts.verify_image_agent_lock import portable_file_bytes, sha256_file


class ImageAgentReleaseLockTests(unittest.TestCase):
    def test_checked_in_lock_is_strict_and_complete(self) -> None:
        release = load_image_agent_lock(default_image_agent_lock_path())

        self.assertEqual(
            release.revision, "8f26bda6eff4fa9c2c5b20498755b329ac7995b5"
        )
        self.assertEqual(release.package_version, "1.8.2")
        self.assertEqual(release.embedded_path, "agents/image_agent_mvp")
        self.assertEqual(len(release.dependency_files), 4)
        self.assertEqual(
            release.runtime_dependency_tree_sha256,
            {
                "linux-x86_64": "1bb3aace0b0ade79ae43f32bbf65551acec8de0e090e75d8cd5173ab74b969bb",
                "windows-amd64": "b683e0e9bc14d7fb53203dd7c264e4741cf415642a7a0e5ef177ddba6dda607e",
            }[runtime_platform_key()],
        )
        self.assertTrue(all(len(item.sha256) == 64 for item in release.dependency_files))

    def test_runtime_platform_keys_normalize_supported_architecture_names(self) -> None:
        self.assertEqual(
            runtime_platform_key(system="Linux", machine="x86_64"), "linux-x86_64"
        )
        self.assertEqual(
            runtime_platform_key(system="Windows", machine="AMD64"), "windows-amd64"
        )
        with self.assertRaises(HarnessError):
            runtime_platform_key(system="Darwin", machine="arm64")

    def test_checked_in_lock_selects_the_windows_runtime_digest(self) -> None:
        with (
            patch("harness.adapters.image_lock.platform.system", return_value="Windows"),
            patch("harness.adapters.image_lock.platform.machine", return_value="AMD64"),
        ):
            release = load_image_agent_lock(default_image_agent_lock_path())

        self.assertEqual(
            release.runtime_dependency_tree_sha256,
            "b683e0e9bc14d7fb53203dd7c264e4741cf415642a7a0e5ef177ddba6dda607e",
        )

    def test_unknown_lock_field_fails_closed(self) -> None:
        document = json.loads(default_image_agent_lock_path().read_text(encoding="utf-8"))
        document["floating_branch"] = "main"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "image-agent.lock.json"
            path.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaises(HarnessError) as rejected:
                load_image_agent_lock(path)

        self.assertEqual(rejected.exception.code, "ADAPTER_UNAVAILABLE")
        self.assertIn("fields", rejected.exception.message)

    def test_release_attestation_normalizes_only_utf8_text_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            linux = root / "linux.json"
            windows = root / "windows.json"
            binary_lf = root / "linux.bin"
            binary_crlf = root / "windows.bin"
            linux.write_bytes(b'{"locked": true}\n')
            windows.write_bytes(b'{"locked": true}\r\n')
            binary_lf.write_bytes(b"\x00locked\n")
            binary_crlf.write_bytes(b"\x00locked\r\n")

            self.assertEqual(portable_file_bytes(windows), linux.read_bytes())
            self.assertEqual(sha256_file(linux), sha256_file(windows))
            self.assertNotEqual(sha256_file(binary_lf), sha256_file(binary_crlf))


if __name__ == "__main__":
    unittest.main()
