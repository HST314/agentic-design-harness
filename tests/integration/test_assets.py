from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from harness.core.errors import HarnessError, SimulatedCrash
from harness.services import asset_reader
from harness.services.assets import AssetService, file_digest
from harness.storage.ndjson import recover_records
from PIL import Image
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
        self.assertNotIn("path", download)
        self.assertEqual(download["stream"].read(), b"# Registered brief\n")
        download["stream"].close()

    def test_shared_archive_zips_every_regular_file(self) -> None:
        task_root = self.store.layout.workspace_root / "tasks" / "t_assets"
        shared = task_root / "resources" / "shared"
        (shared / "bundle_1.png").write_bytes(b"\x89PNG fake image")
        (shared / "bundle_1.md").write_text("# 设计理念\n", encoding="utf-8")
        notes = shared / "notes"
        notes.mkdir()
        (notes / "agent-log.txt").write_text("agent 写入的文档\n", encoding="utf-8")
        (shared / ".pub_inflight.tmp").write_bytes(b"partial publication")
        state = shared / ".general-agent-state"
        state.mkdir()
        (state / "state_secret.json").write_text("{}", encoding="utf-8")
        (notes / ".hidden.md").write_text("internal\n", encoding="utf-8")
        if hasattr(os, "symlink"):
            (shared / "link.png").symlink_to(shared / "bundle_1.png")

        archive = self.assets.download_shared_archive("t_assets")

        self.assertEqual(archive["filename"], "Task t_assets-shared.zip")
        with zipfile.ZipFile(io.BytesIO(archive["content"])) as zipped:
            self.assertEqual(
                zipped.namelist(),
                ["bundle_1.md", "bundle_1.png", "notes/agent-log.txt"],
            )
            self.assertEqual(zipped.read("bundle_1.md").decode("utf-8"), "# 设计理念\n")
            self.assertEqual(
                zipped.read("notes/agent-log.txt").decode("utf-8"),
                "agent 写入的文档\n",
            )

    def test_shared_listing_matches_archive_for_uncommitted_files(self) -> None:
        task_root = self.store.layout.workspace_root / "tasks" / "t_assets"
        shared = task_root / "resources" / "shared"
        (shared / "学院介绍.md").write_text("# 介绍\n", encoding="utf-8")
        notes = shared / "notes"
        notes.mkdir()
        (notes / "agent-log.txt").write_text("agent 写入的文档\n", encoding="utf-8")
        state = shared / ".general-agent-state"
        state.mkdir()
        (state / "state_secret.json").write_text("{}", encoding="utf-8")
        (notes / ".hidden.md").write_text("internal\n", encoding="utf-8")

        listed = {
            item["relative_path"]
            for item in self.assets.list_files("t_assets", "shared")
            if item["relative_path"].startswith("resources/shared/")
        }
        self.assertEqual(
            listed,
            {"resources/shared/学院介绍.md", "resources/shared/notes/agent-log.txt"},
        )

        archive = self.assets.download_shared_archive("t_assets")
        with zipfile.ZipFile(io.BytesIO(archive["content"])) as zipped:
            self.assertEqual(
                {f"resources/shared/{name}" for name in zipped.namelist()},
                listed,
            )

    def test_download_serves_uncommitted_shared_file_but_not_hidden_ones(self) -> None:
        task_root = self.store.layout.workspace_root / "tasks" / "t_assets"
        shared = task_root / "resources" / "shared"
        (shared / "学院介绍.md").write_text("# 介绍\n", encoding="utf-8")
        state = shared / ".general-agent-state"
        state.mkdir()
        (state / "state_secret.json").write_text("{}", encoding="utf-8")

        download = self.assets.download("t_assets", "resources/shared/学院介绍.md")
        self.assertEqual(download["stream"].read(), "# 介绍\n".encode())
        download["stream"].close()
        self.assertEqual(download["mime_type"], "text/markdown")

        with self.assertRaises(HarnessError) as hidden:
            self.assets.download(
                "t_assets", "resources/shared/.general-agent-state/state_secret.json"
            )
        self.assertEqual(hidden.exception.code, "ASSET_VALIDATION_FAILED")
        with self.assertRaises(HarnessError) as missing:
            self.assets.download("t_assets", "resources/shared/absent.md")
        self.assertEqual(missing.exception.code, "ASSET_VALIDATION_FAILED")

    @unittest.skipIf(os.name == "nt", "Windows denies replacement of an open asset")
    def test_preview_and_download_keep_the_verified_inode_during_symlink_swap(self) -> None:
        imported = self.assets.import_bytes(
            "t_assets",
            filename="race.md",
            content=b"# committed content\n",
            description="Race target",
            source="user_upload",
            idempotency_key="import-race-target",
        )
        task_root = self.store.layout.workspace_root / "tasks" / "t_assets"
        target = task_root / imported["relative_path"]
        backup = target.with_name("race.backup")
        outside = self.root / "outside-secret.md"
        outside.write_bytes(b"# outside content must never be returned\n")
        original_detect = asset_reader.detect_mime_stream

        def swap_after_open(stream, filename):
            target.rename(backup)
            target.symlink_to(outside)
            return original_detect(stream, filename)

        with patch.object(asset_reader, "detect_mime_stream", side_effect=swap_after_open):
            preview = self.assets.preview("t_assets", imported["relative_path"])
        self.assertEqual(preview["content"], "# committed content\n")
        target.unlink()
        backup.rename(target)

        with patch.object(asset_reader, "detect_mime_stream", side_effect=swap_after_open):
            download = self.assets.download("t_assets", imported["relative_path"])
        try:
            self.assertEqual(download["stream"].read(), b"# committed content\n")
        finally:
            download["stream"].close()
            target.unlink()
            backup.rename(target)

    def test_file_listing_does_not_offer_a_preview_that_the_endpoint_rejects(self) -> None:
        self.assets.preview_limit_bytes = 8
        imported = self.assets.import_bytes(
            "t_assets",
            filename="large-preview.md",
            content=b"# larger than preview limit\n",
            description="Preview contract boundary",
            source="user_upload",
            idempotency_key="large-preview-boundary",
        )

        file_entry = next(
            item
            for item in self.assets.list_files("t_assets", "inputs")
            if item["relative_path"] == imported["relative_path"]
        )
        self.assertFalse(file_entry["previewable"])
        with self.assertRaises(HarnessError) as rejected:
            self.assets.preview("t_assets", imported["relative_path"])
        self.assertEqual(rejected.exception.code, "ASSET_VALIDATION_FAILED")

    def test_images_use_the_larger_preview_limit_while_text_stays_small(self) -> None:
        self.assets.preview_limit_bytes = 8
        self.assets.image_preview_limit_bytes = 32
        small_image = self.assets.import_bytes(
            "t_assets",
            filename="small.png",
            content=b"\x89PNG\r\n\x1a\n" + b"a" * 8,
            description="Over the text cap, under the image cap",
            source="user_upload",
            idempotency_key="small-image-preview",
        )
        large_image = self.assets.import_bytes(
            "t_assets",
            filename="large.png",
            content=b"\x89PNG\r\n\x1a\n" + b"b" * 64,
            description="Over the image cap",
            source="user_upload",
            idempotency_key="large-image-preview",
        )

        entries = {
            item["relative_path"]: item
            for item in self.assets.list_files("t_assets", "inputs")
        }
        self.assertTrue(entries[small_image["relative_path"]]["previewable"])
        self.assertFalse(entries[large_image["relative_path"]]["previewable"])

        preview = self.assets.preview("t_assets", small_image["relative_path"])
        self.assertEqual(preview["mime_type"], "image/png")
        with self.assertRaises(HarnessError) as rejected:
            self.assets.preview("t_assets", large_image["relative_path"])
        self.assertEqual(rejected.exception.code, "ASSET_VALIDATION_FAILED")

    def test_image_thumbnail_is_resized_and_flattened_for_gallery_use(self) -> None:
        self.assets.max_file_bytes = 64 * 1024
        self.assets.max_task_bytes = 64 * 1024
        source = io.BytesIO()
        Image.new("RGBA", (1200, 600), (255, 0, 0, 128)).save(source, format="PNG")
        imported = self.assets.import_bytes(
            "t_assets",
            filename="wide.png",
            content=source.getvalue(),
            description="Gallery thumbnail source",
            source="user_upload",
            idempotency_key="gallery-thumbnail",
        )

        preview = self.assets.preview(
            "t_assets", imported["relative_path"], thumbnail=True
        )

        self.assertEqual(preview["mime_type"], "image/jpeg")
        with Image.open(io.BytesIO(preview["content"])) as thumbnail:
            self.assertEqual(thumbnail.mode, "RGB")
            self.assertEqual(thumbnail.size, (640, 320))

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

    def test_preseeded_publication_target_cannot_be_committed(self) -> None:
        candidate = self.instance_root / "outputs" / "candidate.png"
        candidate.write_bytes(b"\x89PNG\r\n\x1a\ninstance-candidate")
        idempotency_key = "publish-preseeded"
        transaction_digest = hashlib.sha256(
            f"i_image_1:{idempotency_key}".encode()
        ).hexdigest()
        publication_id = f"pub_{transaction_digest[:24]}"
        asset_digest = hashlib.sha256(
            f"t_assets:{publication_id}".encode()
        ).hexdigest()
        asset_id = f"a_pub_{asset_digest[:20]}"
        destination = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_assets"
            / "resources"
            / "shared"
            / asset_id
            / "candidate.png"
        )
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"\x89PNG\r\n\x1a\npreseeded-hostile-content")

        with self.assertRaises(HarnessError):
            self.assets.publish_delivery(
                "t_assets",
                "i_image_1",
                source_relative_path="instances/i_image_1/outputs/candidate.png",
                role="final_artwork",
                description="Must come from the instance candidate",
                idempotency_key=idempotency_key,
            )

        self.assertEqual(
            destination.read_bytes(), b"\x89PNG\r\n\x1a\npreseeded-hostile-content"
        )
        self.assertFalse(
            any(
                event.get("publication_id") == publication_id
                for event in recover_records(self.assets._event_path("t_assets"))
            )
        )

    def test_browser_rejects_uncommitted_files_outside_the_shared_area(self) -> None:
        task_root = self.store.layout.workspace_root / "tasks" / "t_assets"
        private_candidate = self.instance_root / "outputs" / "draft.png"
        private_candidate.write_bytes(b"\x89PNG\r\n\x1a\nuncommitted-output")
        forged_manifest = task_root / "resources" / "manifests" / "a_forged.json"
        forged_manifest.write_text("{}", encoding="utf-8")

        listed = {
            item["relative_path"] for item in self.assets.list_files("t_assets", "all")
        }
        for relative_path in (
            "instances/i_image_1/outputs/draft.png",
            "resources/manifests/a_forged.json",
        ):
            self.assertNotIn(relative_path, listed)
            with self.assertRaises(HarnessError):
                self.assets.preview("t_assets", relative_path)
            with self.assertRaises(HarnessError):
                self.assets.download("t_assets", relative_path)

        # The shared folder is the user-facing product area: files dropped
        # there are listed and downloadable (exactly like the shared zip),
        # but preview stays on the verified committed path.
        forged_public = task_root / "resources" / "shared" / "a_forged" / "forged.png"
        forged_public.parent.mkdir(parents=True)
        forged_public.write_bytes(b"\x89PNG\r\n\x1a\nuncommitted-shared")
        self.assertIn(
            "resources/shared/a_forged/forged.png",
            {
                item["relative_path"]
                for item in self.assets.list_files("t_assets", "all")
            },
        )
        with self.assertRaises(HarnessError):
            self.assets.preview("t_assets", "resources/shared/a_forged/forged.png")
        download = self.assets.download("t_assets", "resources/shared/a_forged/forged.png")
        self.assertEqual(download["stream"].read(), b"\x89PNG\r\n\x1a\nuncommitted-shared")
        download["stream"].close()

        committed = self.assets.import_bytes(
            "t_assets",
            filename="verified.md",
            content=b"# verified before tampering\n",
            description="Live verification target",
            source="user_upload",
            idempotency_key="browser-live-verification",
        )
        committed_path = task_root / committed["relative_path"]
        committed_path.chmod(0o640)
        committed_path.write_text("tampered after commit", encoding="utf-8")
        self.assertNotIn(
            committed["relative_path"],
            {
                item["relative_path"]
                for item in self.assets.list_files("t_assets", "inputs")
            },
        )
        with self.assertRaises(HarnessError) as corrupted:
            self.assets.preview("t_assets", committed["relative_path"])
        self.assertEqual(corrupted.exception.code, "ASSET_CORRUPTED")

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
