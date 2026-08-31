from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import PptAgentAdapter, PrepareRequest
from harness.adapters.ppt_lock import load_ppt_agent_lock
from harness.storage.atomic import atomic_write_json

from tests.runtime_helpers import build_store

ROOT = Path(__file__).resolve().parents[2]


class PptAgentAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = build_store(self.root)
        self.adapter = PptAgentAdapter(
            self.store,
            self.store.contracts,
            source_root=ROOT / "agents" / "ppt-agent",
            interpreter=Path(sys.executable).resolve(),
            dependency_root=ROOT / ".runtime" / "ppt-agent-deps",
            release_lock=load_ppt_agent_lock(ROOT / "agents" / "ppt-agent.lock.json"),
            runtime_policy=ROOT / "config" / "ppt_agent_runtime.yaml",
            model_config=ROOT / "config" / "ppt_agent_model_config.yaml",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepare_maps_card_and_injects_isolated_runtime_paths(self) -> None:
        task_id = "task_ppt"
        instance_id = "i_ppt"
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        card = {
            "schema_version": "1.0",
            "card_id": "card_ppt",
            "revision": 1,
            "task_id": task_id,
            "stage_id": "stage_ppt",
            "instance_id": instance_id,
            "agent_type": "ppt",
            "objective": "制作品牌发布会演示文稿",
            "instructions": ["突出品牌升级", "使用已确认图片"],
            "input_assets": [],
            "expected_deliveries": [
                {
                    "kind": "archive",
                    "role": "html_ppt",
                    "required": True,
                    "accepted_mime_types": ["application/zip"],
                }
            ],
            "parameters": {"slide_count": 12},
            "created_at": "2026-08-27T00:00:00Z",
        }
        instance = {
            "instance_id": instance_id,
            "task_id": task_id,
            "stage_id": "stage_ppt",
            "agent_type": "ppt",
        }
        spec = self.adapter.prepare(
            PrepareRequest(instance, card, task_root, task_root / "unused.yaml")
        )
        self.assertEqual(spec.health_path, "/api/health")
        self.assertEqual(spec.ui_path, "/")
        self.assertIn("{port}", spec.command)
        self.assertEqual(
            spec.public_environment["PPT_AGENT_IMAGES_ROOT"],
            str(task_root / "instances" / instance_id / "work" / "input-snapshot"),
        )
        self.assertEqual(
            spec.writable_roots,
            (
                task_root / "instances" / instance_id / "work" / "projects",
                ROOT / "config",
            ),
        )
        self.assertEqual(
            spec.public_environment["PPT_AGENT_RUNTIME_POLICY"],
            str(ROOT / "config" / "ppt_agent_runtime.yaml"),
        )
        self.assertEqual(
            spec.public_environment["PPT_AGENT_MODEL_CONFIG"],
            str(ROOT / "config" / "ppt_agent_model_config.yaml"),
        )
        self.assertEqual(
            spec.public_environment["PPT_AGENT_PROJECTS_ROOT"],
            str(task_root / "instances" / instance_id / "work" / "projects"),
        )
        self.assertEqual(
            spec.public_environment["PPT_AGENT_MANAGED_PROJECT_ID"], instance_id
        )
        self.assertEqual(spec.public_environment["HARNESS_TASK_ID"], task_id)
        self.assertEqual(spec.public_environment["HARNESS_INSTANCE_ID"], instance_id)
        mapped = self.adapter.map_task_card(
            PrepareRequest(instance, card, task_root, task_root / "unused.yaml")
        )
        self.assertEqual(mapped["target_slide_count"], "12")
        self.assertEqual(mapped["objective"], card["objective"])
        self.assertEqual(mapped["constraints"], card["instructions"])

    def test_prepare_reuses_startup_verified_runtime_identity(self) -> None:
        task_id = "task_cached_identity"
        instance_id = "i_ppt_cached_identity"
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        card = {
            "schema_version": "1.0",
            "card_id": "card_ppt_cached_identity",
            "revision": 1,
            "task_id": task_id,
            "stage_id": "stage_ppt",
            "instance_id": instance_id,
            "agent_type": "ppt",
            "objective": "Create a cached identity deck",
            "instructions": [],
            "input_assets": [],
            "expected_deliveries": [
                {
                    "kind": "archive",
                    "role": "html_ppt",
                    "required": True,
                    "accepted_mime_types": ["application/zip"],
                }
            ],
            "parameters": {"slide_count": 6, "input_source": "empty"},
            "created_at": "2026-08-28T00:00:00Z",
        }
        instance = {
            "instance_id": instance_id,
            "task_id": task_id,
            "stage_id": "stage_ppt",
            "agent_type": "ppt",
        }

        with (
            patch.object(self.adapter, "_validate_runtime") as validate_runtime,
            patch("harness.adapters.ppt.verify_ppt_runtime_identity") as identity,
        ):
            spec = self.adapter.prepare(
                PrepareRequest(instance, card, task_root, task_root / "unused.yaml")
            )

        validate_runtime.assert_not_called()
        identity.assert_not_called()
        self.assertIs(spec.verified_runtime_identity, self.adapter.runtime_identity)

    def test_recovery_replays_start_when_managed_project_was_not_created(self) -> None:
        task_id = "task_ppt_recovery"
        instance_id = "i_ppt_recovery"
        atomic_write_json(
            self.adapter._state_path(task_id, instance_id),
            {"project_created": False},
        )

        with patch.object(self.adapter, "get_status") as get_status:
            recovery = self.adapter.recover(
                {
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "status": "READY",
                }
            )

        self.assertFalse(recovery.recovered)
        self.assertEqual(recovery.status, "READY")
        self.assertEqual(recovery.details["mode"], "idempotent_start_replay")
        get_status.assert_not_called()

    def test_prepare_empty_input_uses_an_instance_private_empty_directory(self) -> None:
        task_id = "task_empty"
        instance_id = "i_ppt_empty"
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        card = {
            "schema_version": "1.0",
            "card_id": "card_ppt_empty",
            "revision": 1,
            "task_id": task_id,
            "stage_id": "stage_ppt",
            "instance_id": instance_id,
            "agent_type": "ppt",
            "objective": "Create a text-only deck",
            "instructions": [],
            "input_assets": [],
            "expected_deliveries": [
                {
                    "kind": "archive",
                    "role": "html_ppt",
                    "required": True,
                    "accepted_mime_types": ["application/zip"],
                }
            ],
            "parameters": {"slide_count": 8, "input_source": "empty"},
            "created_at": "2026-08-27T00:00:00Z",
        }
        instance = {
            "instance_id": instance_id,
            "task_id": task_id,
            "stage_id": "stage_ppt",
            "agent_type": "ppt",
        }

        spec = self.adapter.prepare(
            PrepareRequest(instance, card, task_root, task_root / "unused.yaml")
        )

        images_root = Path(spec.public_environment["PPT_AGENT_IMAGES_ROOT"])
        self.assertEqual(
            images_root,
            task_root / "instances" / instance_id / "work" / "input-snapshot",
        )
        self.assertTrue(images_root.is_dir())
        self.assertEqual(list(images_root.iterdir()), [])
        self.assertNotEqual(images_root, task_root / "resources" / "shared")


if __name__ == "__main__":
    unittest.main()
