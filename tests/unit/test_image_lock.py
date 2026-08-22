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


class ImageAgentReleaseLockTests(unittest.TestCase):
    def test_checked_in_lock_is_strict_and_complete(self) -> None:
        release = load_image_agent_lock(default_image_agent_lock_path())

        self.assertEqual(
            release.revision, "4a958099e0452190f7bdd576e8e8a3a4f54c6000"
        )
        self.assertEqual(release.package_version, "1.8.2")
        self.assertEqual(release.embedded_path, "agents/image_agent_mvp")
        self.assertEqual(len(release.dependency_files), 4)
        self.assertTrue(all(len(item.sha256) == 64 for item in release.dependency_files))

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


if __name__ == "__main__":
    unittest.main()
