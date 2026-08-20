from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.assets import AssetService, file_digest
from runtime_helpers import build_service, create_task, envelope, image_plan


class AssetServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        created = create_task(self.commands, "t_assets", "auto")
        draft = image_plan("t_assets")
        self.commands.save_plan(
            "t_assets",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope("save-assets", created["revision"]),
        )
        self.assets = AssetService(self.store, max_file_bytes=4096, max_task_bytes=8192)
        self.instance_root = self.assets.initialize_instance_workspace(
            "t_assets", "i_image_1"
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_complete_workspace_import_selection_and_safe_read_interfaces(self) -> None:
        task_root = self.store.layout.workspace_root / "tasks" / "t_assets"
        for relative in (
            "inputs/original",
            "inputs/selected",
            "resources/shared",
            "resources/manifests",
            "instances/i_image_1/work",
            "instances/i_image_1/outputs",
            "instances/i_image_1/logs",
            "approvals",
        ):
            self.assertTrue((task_root / relative).is_dir())
        self.assertTrue((task_root / "task-summary.json").is_file())

        imported = self.assets.import_bytes(
            "t_assets",
            filename="brief.md",
            content=b"# Registered brief\n",
            description="User brief",
            source="user_upload",
            idempotency_key="import-brief",
        )
        replayed = self.assets.import_bytes(
            "t_assets",
            filename="brief.md",
            content=b"# Registered brief\n",
            description="User brief",
            source="user_upload",
            idempotency_key="import-brief",
        )
        self.assertEqual(replayed, imported)
        with self.assertRaises(HarnessError) as conflict:
            self.assets.import_bytes(
                "t_assets",
                filename="brief.md",
                content=b"different",
                description="User brief",
                source="user_upload",
                idempotency_key="import-brief",
            )
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

        selected = self.assets.select_inputs(
            "t_assets", [imported["asset_id"]], manifest_id="input_rev_1"
        )
        self.assertEqual(
            selected["task_card_inputs"],
            [
                {
                    "asset_id": imported["asset_id"],
                    "manifest_relpath": f"inputs/manifests/{imported['asset_id']}.json",
                }
            ],
        )
        self.assertFalse(Path(selected["manifest_relpath"]).is_absolute())

        preview = self.assets.preview("t_assets", imported["relative_path"])
        self.assertEqual(preview["mime_type"], "text/markdown")
        self.assertIn("Registered brief", preview["content"])
        download = self.assets.download("t_assets", imported["relative_path"])
        self.assertEqual(download["headers"]["X-Content-Type-Options"], "nosniff")
        self.assertTrue(download["headers"]["Content-Disposition"].startswith("attachment;"))

    def test_path_traversal_symlinks_and_unknown_mime_are_rejected(self) -> None:
        with self.assertRaises(HarnessError) as absolute:
            self.assets.preview("t_assets", "/etc/passwd")
        self.assertEqual(absolute.exception.code, "PATH_OUTSIDE_TASK_ROOT")
        with self.assertRaises(HarnessError) as traversal:
            self.assets.preview("t_assets", "inputs/../task-summary.json")
        self.assertEqual(traversal.exception.code, "PATH_OUTSIDE_TASK_ROOT")
        with self.assertRaises(HarnessError) as filename:
            self.assets.import_bytes(
                "t_assets",
                filename="../escape.txt",
                content=b"unsafe",
                description="unsafe",
                source="user_upload",
                idempotency_key="unsafe-name",
            )
        self.assertEqual(filename.exception.code, "ASSET_VALIDATION_FAILED")
        with self.assertRaises(HarnessError) as mime:
            self.assets.import_bytes(
                "t_assets",
                filename="payload.bin",
                content=b"\x00\x01\x02\x03",
                description="binary",
                source="user_upload",
                idempotency_key="binary-file",
            )
        self.assertEqual(mime.exception.code, "ASSET_VALIDATION_FAILED")
        originals = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_assets"
            / "inputs"
            / "original"
        )
        self.assertEqual([path for path in originals.rglob("*") if path.is_file()], [])

        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.instance_root / "outputs" / "linked.txt"
        link.symlink_to(outside)
        with self.assertRaises(HarnessError) as symlink:
            self.assets.publish_delivery(
                "t_assets",
                "i_image_1",
                source_relative_path="instances/i_image_1/outputs/linked.txt",
                role="final_artwork",
                description="unsafe",
                idempotency_key="publish-link",
            )
        self.assertEqual(symlink.exception.code, "PATH_OUTSIDE_TASK_ROOT")
        unknown_output = self.instance_root / "outputs" / "payload.bin"
        unknown_output.write_bytes(b"\x00\x01\x02\x03")
        with self.assertRaises(HarnessError) as unknown_publication:
            self.assets.publish_delivery(
                "t_assets",
                "i_image_1",
                source_relative_path="instances/i_image_1/outputs/payload.bin",
                role="final_artwork",
                description="unknown type",
                idempotency_key="publish-unknown",
            )
        self.assertEqual(unknown_publication.exception.code, "ASSET_VALIDATION_FAILED")
        shared = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_assets"
            / "resources"
            / "shared"
        )
        self.assertEqual(list(shared.rglob("*.tmp")), [])
        log_path = self.instance_root / "logs" / "stdout.log"
        log_path.write_text("not a resource", encoding="utf-8")
        with self.assertRaises(HarnessError) as private_log:
            self.assets.download(
                "t_assets", "instances/i_image_1/logs/stdout.log"
            )
        self.assertEqual(private_log.exception.code, "PATH_OUTSIDE_TASK_ROOT")
        listed = self.assets.list_files("t_assets", "instances")
        self.assertNotIn(
            "instances/i_image_1/logs/stdout.log",
            {item["relative_path"] for item in listed},
        )

    def test_controlled_publication_is_the_only_visibility_commit(self) -> None:
        candidate = self.instance_root / "outputs" / "final.png"
        candidate.write_bytes(b"\x89PNG\r\n\x1a\ncontrolled")
        manifest = self.assets.publish_delivery(
            "t_assets",
            "i_image_1",
            source_relative_path="instances/i_image_1/outputs/final.png",
            role="final_artwork",
            description="Final artwork",
            idempotency_key="publish-final",
        )
        task_root = self.store.layout.workspace_root / "tasks" / "t_assets"
        public = task_root / manifest["relative_path"]
        self.assertEqual(file_digest(public), (manifest["size_bytes"], manifest["sha256"]))
        self.assertEqual(manifest["producer_instance_id"], "i_image_1")
        self.assertEqual(manifest["mime_type"], "image/png")
        self.assertEqual(
            self.assets.publish_delivery(
                "t_assets",
                "i_image_1",
                source_relative_path="instances/i_image_1/outputs/final.png",
                role="final_artwork",
                description="Final artwork",
                idempotency_key="publish-final",
            ),
            manifest,
        )

        fake_dir = task_root / "resources" / "shared" / "a_fake"
        fake_dir.mkdir()
        (fake_dir / "fake.png").write_bytes(b"\x89PNG\r\n\x1a\nforged")
        forged = {
            **manifest,
            "asset_id": "a_fake",
            "relative_path": "resources/shared/a_fake/fake.png",
        }
        (task_root / "resources" / "manifests" / "a_fake.json").write_text(
            json.dumps(forged), encoding="utf-8"
        )
        visible_ids = {item["manifest"]["asset_id"] for item in self.assets.list_assets("t_assets")}
        self.assertEqual(visible_ids, {manifest["asset_id"]})

        public.chmod(0o640)
        public.write_bytes(b"tampered")
        with self.assertRaises(HarnessError) as corrupted:
            self.assets.verify_asset("t_assets", manifest["asset_id"])
        self.assertEqual(corrupted.exception.code, "ASSET_CORRUPTED")
        listed = self.assets.list_assets("t_assets")
        self.assertEqual(listed[0]["integrity_status"], "CORRUPTED")

    def test_import_visibility_commit_recovers_a_pre_event_crash(self) -> None:
        def crash_after_prepare(checkpoint: str) -> None:
            if checkpoint == "after_import_prepare":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.assets.import_bytes(
                "t_assets",
                filename="recover.md",
                content=b"# recover import\n",
                description="recoverable import",
                source="user_upload",
                idempotency_key="recover-import",
                crash_hook=crash_after_prepare,
            )
        self.assertEqual(self.assets.list_assets("t_assets"), [])
        self.assets.recover()
        visible = self.assets.list_assets("t_assets")
        self.assertEqual(len(visible), 1)
        replay = self.assets.import_bytes(
            "t_assets",
            filename="recover.md",
            content=b"# recover import\n",
            description="recoverable import",
            source="user_upload",
            idempotency_key="recover-import",
        )
        self.assertEqual(replay["asset_id"], visible[0]["manifest"]["asset_id"])

    def test_publication_recovers_every_visibility_crash_window(self) -> None:
        for index, checkpoint in enumerate(
            ("after_final_rename", "after_manifest_rename", "after_publication_event"),
            start=1,
        ):
            candidate = self.instance_root / "outputs" / f"candidate-{index}.png"
            candidate.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))

            def crash(current: str, target: str = checkpoint) -> None:
                if current == target:
                    raise SimulatedCrash(current)

            with self.assertRaises(SimulatedCrash):
                self.assets.publish_delivery(
                    "t_assets",
                    "i_image_1",
                    source_relative_path=(
                        f"instances/i_image_1/outputs/candidate-{index}.png"
                    ),
                    role="final_artwork",
                    description=f"Candidate {index}",
                    idempotency_key=f"publish-crash-{index}",
                    crash_hook=crash,
                )
            self.assets.recover()
            recovered = self.assets.publish_delivery(
                "t_assets",
                "i_image_1",
                source_relative_path=f"instances/i_image_1/outputs/candidate-{index}.png",
                role="final_artwork",
                description=f"Candidate {index}",
                idempotency_key=f"publish-crash-{index}",
            )
            self.assertEqual(
                self.assets.verify_asset("t_assets", recovered["asset_id"]), recovered
            )


if __name__ == "__main__":
    unittest.main()
