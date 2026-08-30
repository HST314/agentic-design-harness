from __future__ import annotations

import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from runtime_helpers import build_config_snapshot

ROOT = Path(__file__).resolve().parents[2]


class TaskIntakeApiTests(unittest.TestCase):
    def test_draft_upload_replays_an_asset_imported_with_the_legacy_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(
                HarnessSettings(
                    control_root=Path(temporary) / "control-data",
                    workspace_root=Path(temporary) / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    config_snapshot=build_config_snapshot(),
                )
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "Create a launch visual.",
                        "start_policy": "manual",
                        "envelope": self._envelope("create-legacy-replay", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                task_id = created.json()["task"]["task_id"]
                upload_key = "legacy-upload-replay"
                identity = hashlib.sha256(f"{task_id}\0{upload_key}".encode()).hexdigest()
                legacy_manifest = app.state.container.assets.import_stream(
                    task_id,
                    io.BytesIO(b"legacy brief\n"),
                    filename="legacy.txt",
                    description="Description for legacy.txt",
                    source="web_task_intake",
                    idempotency_key=f"intake-asset-{identity[:40]}",
                    max_file_bytes=10 * 1024 * 1024,
                )

                replayed = self._upload(
                    client,
                    task_id,
                    name="legacy.txt",
                    content=b"legacy brief\n",
                    mime="text/plain",
                    key=upload_key,
                    expected=1,
                )

                self.assertEqual(replayed.status_code, 200, replayed.text)
                self.assertEqual(
                    replayed.json()["asset"]["asset_id"], legacy_manifest["asset_id"]
                )
                self.assertEqual(
                    client.get(f"/api/v1/task-intakes/{task_id}").json()["intake"]["asset_ids"],
                    [legacy_manifest["asset_id"]],
                )

    def test_precreation_snapshot_survives_a_failure_before_the_task_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = {
                "prompt": "Create a launch visual.",
                "start_policy": "manual",
                "envelope": self._envelope("pin-before-task-fact", 0),
            }
            initial_app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    config_snapshot=build_config_snapshot(revision="cfg_before_failure"),
                )
            )
            with TestClient(initial_app) as client, patch.object(
                initial_app.state.container.commands,
                "create_task",
                side_effect=RuntimeError("simulated process loss"),
            ), self.assertRaises(RuntimeError):
                client.post("/api/v1/task-intakes", json=request)

            restarted_app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    config_snapshot=build_config_snapshot(revision="cfg_after_failure"),
                )
            )
            with TestClient(restarted_app) as client:
                created = client.post("/api/v1/task-intakes", json=request)
                self.assertEqual(created.status_code, 200, created.text)
                task_id = created.json()["task"]["task_id"]
                pinned = restarted_app.state.container.task_config.get_public(task_id)

            self.assertEqual(pinned["source_config_revision"], "cfg_before_failure")

    def test_creation_pins_config_before_a_process_restart_can_change_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initial_settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
                config_snapshot=build_config_snapshot(revision="cfg_creation_snapshot"),
            )
            with TestClient(create_app(initial_settings)) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "Create a launch visual.",
                        "start_policy": "manual",
                        "envelope": self._envelope("pin-at-creation", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                task_id = created.json()["task"]["task_id"]

            restarted_settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
                config_snapshot=build_config_snapshot(revision="cfg_restart_snapshot"),
            )
            restarted_app = create_app(restarted_settings)
            with TestClient(restarted_app):
                pinned = restarted_app.state.container.task_config.get_public(task_id)

            self.assertEqual(pinned["source_config_revision"], "cfg_creation_snapshot")

    def test_recoverable_upload_submit_and_presentation_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = HarnessSettings(
                control_root=root / "control-data",
                workspace_root=root / "workspace",
                contracts_root=ROOT / "contracts" / "v1",
                config_snapshot=build_config_snapshot(),
            )
            app = create_app(settings)
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "为秋季发布会制作三套主视觉方向。",
                        "start_policy": "manual",
                        "envelope": self._envelope("create-intake", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                task_id = created.json()["task"]["task_id"]
                replay = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "为秋季发布会制作三套主视觉方向。",
                        "start_policy": "manual",
                        "envelope": self._envelope("create-intake", 0),
                    },
                )
                self.assertEqual(replay.json(), created.json())

                first = self._upload(
                    client,
                    task_id,
                    name="brief.md",
                    content=b"# launch brief\n",
                    mime="text/markdown",
                    key="upload-brief",
                    expected=1,
                )
                self.assertEqual(first.status_code, 200, first.text)
                # Independent uploads are a commutative mutation, so three browser
                # workers may safely carry the same observed revision.
                second = self._upload(
                    client,
                    task_id,
                    name="notes.txt",
                    content=b"brand constraints\n",
                    mime="text/plain",
                    key="upload-notes",
                    expected=1,
                )
                self.assertEqual(second.status_code, 200, second.text)
                self.assertEqual(second.json()["intake_revision"], 3)
                recovered_assets = client.get(
                    f"/api/v1/task-intakes/{task_id}"
                ).json()["assets"]
                self.assertEqual(len(recovered_assets), 2)

                removed_asset = first.json()["asset"]["asset_id"]
                removed = client.request(
                    "DELETE",
                    f"/api/v1/task-intakes/{task_id}/assets/{removed_asset}",
                    json={"envelope": self._envelope("remove-brief", 3)},
                )
                self.assertEqual(removed.status_code, 200, removed.text)
                self.assertEqual(
                    removed.json()["intake"]["asset_ids"],
                    [second.json()["asset"]["asset_id"]],
                )

                invalid = self._upload(
                    client,
                    task_id,
                    name="pretend.md",
                    content=b"%PDF-1.7\n",
                    mime="text/markdown",
                    key="upload-invalid-mime",
                    expected=4,
                )
                self.assertEqual(invalid.status_code, 422, invalid.text)
                self.assertEqual(invalid.json()["error"]["code"], "ASSET_VALIDATION_FAILED")

                submitted = client.post(
                    f"/api/v1/task-intakes/{task_id}/submit",
                    json={
                        "task_expected_revision": 1,
                        "envelope": self._envelope("submit-intake", 4),
                    },
                )
                self.assertEqual(submitted.status_code, 200, submitted.text)
                self.assertEqual(submitted.json()["intake"]["status"], "SUBMITTED")
                self.assertEqual(submitted.json()["intake"]["upload_session"]["status"], "LOCKED")
                self.assertTrue(
                    submitted.json()["task"]["input_manifest"].startswith(
                        "inputs/manifests/selected_"
                    )
                )
                closed = self._upload(
                    client,
                    task_id,
                    name="late.txt",
                    content=b"late",
                    mime="text/plain",
                    key="upload-late",
                    expected=5,
                )
                self.assertEqual(closed.status_code, 409, closed.text)
                late = client.post(
                    f"/api/v1/tasks/{task_id}/asset-uploads",
                    files={"file": ("late.txt", b"late", "text/plain")},
                    data={
                        "declared_mime_type": "text/plain",
                        "description": "Late task resource",
                        "idempotency_key": "upload-late-task-resource",
                        "actor_id": "human_operator",
                        "expected_revision": "5",
                    },
                )
                self.assertEqual(late.status_code, 200, late.text)
                self.assertEqual(late.json()["intake"]["status"], "SUBMITTED")
                self.assertEqual(late.json()["intake_revision"], 6)
                master_assets = client.get(
                    f"/api/v1/tasks/{task_id}/master/messages"
                ).json()["assets"]
                self.assertEqual(
                    [asset["filename"] for asset in master_assets],
                    ["notes.txt", "late.txt"],
                )

                renamed = client.patch(
                    f"/api/v1/tasks/{task_id}/presentation",
                    json={
                        "title": "秋季发布会主视觉",
                        "envelope": self._envelope("rename-task", 2),
                    },
                )
                self.assertEqual(renamed.status_code, 200, renamed.text)
                pinned = client.patch(
                    f"/api/v1/tasks/{task_id}/presentation",
                    json={
                        "pinned": True,
                        "envelope": self._envelope("pin-task", 1),
                    },
                )
                self.assertIsNotNone(pinned.json()["navigation"]["pinned_at"])
                archived = client.patch(
                    f"/api/v1/tasks/{task_id}/presentation",
                    json={
                        "archived": True,
                        "envelope": self._envelope("archive-task", 2),
                    },
                )
                self.assertIsNone(archived.json()["navigation"]["pinned_at"])
                self.assertEqual(archived.json()["task"]["status"], "DRAFT")
                listing = client.get("/api/v1/tasks").json()["items"][0]
                self.assertEqual(listing["title"], "秋季发布会主视觉")
                self.assertIsNotNone(listing["archived_at"])

            recovered_app = create_app(settings)
            with TestClient(recovered_app) as recovered_client:
                recovered = recovered_client.get(f"/api/v1/task-intakes/{task_id}")
                self.assertEqual(recovered.status_code, 200, recovered.text)
                self.assertEqual(recovered.json()["intake"]["status"], "SUBMITTED")
                self.assertEqual(
                    [item["filename"] for item in recovered.json()["assets"]],
                    ["notes.txt", "late.txt"],
                )

    def test_prompt_has_no_product_character_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    config_snapshot=build_config_snapshot(),
                )
            )
            prompt = "长" * 20_001
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": prompt,
                        "start_policy": "manual",
                        "envelope": self._envelope("create-long-intake", 0),
                    },
                )
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["intake"]["prompt"], prompt)
            self.assertEqual(created.json()["task"]["goal"], prompt)

    def test_late_upload_route_does_not_shadow_the_json_import_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    config_snapshot=build_config_snapshot(),
                )
            )
            with TestClient(app) as client:
                created = client.post(
                    "/api/v1/task-intakes",
                    json={
                        "prompt": "Create a launch visual.",
                        "start_policy": "manual",
                        "envelope": self._envelope("create-for-route-split", 0),
                    },
                )
                self.assertEqual(created.status_code, 200, created.text)
                task_id = created.json()["task"]["task_id"]

                revision = app.state.container.store.task.revision(task_id, task_id)
                imported = client.post(
                    f"/api/v1/tasks/{task_id}/assets",
                    json={
                        "filename": "imported.md",
                        "content_base64": base64.b64encode(b"# imported\n").decode("ascii"),
                        "description": "JSON import stays on the original route",
                        "operation_id": "import_after_route_split",
                        "envelope": self._envelope("import-via-json", revision),
                    },
                )
                self.assertEqual(imported.status_code, 200, imported.text)
                self.assertTrue(
                    imported.json()["manifest"]["relative_path"].endswith("/imported.md")
                )

                revision = app.state.container.store.task.revision(task_id, task_id)
                uploaded = client.post(
                    f"/api/v1/tasks/{task_id}/asset-uploads",
                    files={"file": ("uploaded.md", b"# uploaded\n", "text/markdown")},
                    data={
                        "declared_mime_type": "text/markdown",
                        "description": "Multipart upload on its own route",
                        "idempotency_key": "upload-after-json-import",
                        "actor_id": "human_operator",
                        "expected_revision": str(revision),
                    },
                )
                self.assertEqual(uploaded.status_code, 200, uploaded.text)
                self.assertEqual(uploaded.json()["asset"]["filename"], "uploaded.md")

    @staticmethod
    def _upload(
        client: TestClient,
        task_id: str,
        *,
        name: str,
        content: bytes,
        mime: str,
        key: str,
        expected: int,
    ):
        return client.post(
            f"/api/v1/task-intakes/{task_id}/assets",
            files={"file": (name, content, mime)},
            data={
                "declared_mime_type": mime,
                "description": f"Description for {name}",
                "idempotency_key": key,
                "actor_id": "human_operator",
                "expected_revision": str(expected),
            },
        )

    @staticmethod
    def _envelope(key: str, expected_revision: int) -> dict[str, object]:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "human_operator",
            "expected_revision": expected_revision,
        }


if __name__ == "__main__":
    unittest.main()
