from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.adapters.image_lock import (
    default_image_agent_lock_path,
    load_image_agent_lock,
)
from harness.core.errors import HarnessError

from scripts.verify_image_agent_lock import portable_file_bytes, sha256_file


class ImageAgentReleaseLockTests(unittest.TestCase):
    def test_checked_in_lock_is_strict_and_complete(self) -> None:
        release = load_image_agent_lock(default_image_agent_lock_path())

        self.assertEqual(
            release.revision, "d33fba9e9afe1fb98f88f60a00889fb9b6691a7b"
        )
        self.assertEqual(release.package_version, "1.8.5")
        self.assertEqual(release.embedded_path, "agents/image_agent_mvp")
        self.assertEqual(len(release.dependency_files), 4)
        self.assertTrue(all(len(item.sha256) == 64 for item in release.dependency_files))
        document = json.loads(
            default_image_agent_lock_path().read_text(encoding="utf-8")
        )
        self.assertEqual(
            set(document["dependencies"]), {"files", "lock_set_sha256"}
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
