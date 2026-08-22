from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import HarnessError
from harness.services.assets import AssetService
from runtime_helpers import build_service, create_task, envelope, image_plan


class DeliveryCandidatePreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        created = create_task(self.commands, "t_preview", "manual")
        plan = image_plan("t_preview")
        self.commands.save_plan(
            "t_preview",
            stages=plan["stages"],
            instances=plan["instances"],
            task_cards=plan["task_cards"],
            envelope=envelope("save-preview-plan", created["revision"]),
        )
        self.assets = AssetService(self.store)
        self.instance_root = self.store.layout.initialize_instance(
            "t_preview", "i_image_1"
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_private_image_and_markdown_are_live_verified_before_preview(self) -> None:
        image = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        note = b"# Frozen branch\n\n- Image and note stay paired.\n"
        outputs = self.instance_root / "outputs"
        image_path = outputs / "bundle-preview.png"
        note_path = outputs / "bundle-preview.md"
        image_path.write_bytes(image)
        note_path.write_bytes(note)
        image_descriptor = {
            "private_relative_path": "instances/i_image_1/outputs/bundle-preview.png",
            "mime_type": "image/png",
            "size_bytes": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
        }
        note_descriptor = {
            "private_relative_path": "instances/i_image_1/outputs/bundle-preview.md",
            "mime_type": "text/markdown",
            "size_bytes": len(note),
            "sha256": hashlib.sha256(note).hexdigest(),
        }
        image_preview = self.assets.preview_delivery_candidate(
            "t_preview", "i_image_1", image_descriptor
        )
        note_preview = self.assets.preview_delivery_candidate(
            "t_preview", "i_image_1", note_descriptor
        )
        self.assertEqual(image_preview["content"], image)
        self.assertEqual(note_preview["content"], note.decode())

        note_path.write_text("changed", encoding="utf-8")
        with self.assertRaises(HarnessError) as corrupted:
            self.assets.preview_delivery_candidate(
                "t_preview", "i_image_1", note_descriptor
            )
        self.assertEqual(corrupted.exception.code, "ASSET_CORRUPTED")


if __name__ == "__main__":
    unittest.main()
