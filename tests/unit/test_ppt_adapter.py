from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from harness.adapters import PptAgentAdapter, PrepareRequest
from harness.adapters.ppt_lock import load_ppt_agent_lock

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
            str(task_root / "resources" / "shared"),
        )
        self.assertEqual(
            spec.public_environment["PPT_AGENT_PROJECTS_ROOT"],
            str(task_root / "instances" / instance_id / "work" / "projects"),
        )
        mapped = self.adapter.map_task_card(
            PrepareRequest(instance, card, task_root, task_root / "unused.yaml")
        )
        self.assertEqual(mapped["target_slide_count"], "12")
        self.assertEqual(mapped["objective"], card["objective"])
        self.assertEqual(mapped["constraints"], card["instructions"])

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
            task_root / "instances" / instance_id / "work" / "empty-input",
        )
        self.assertTrue(images_root.is_dir())
        self.assertEqual(list(images_root.iterdir()), [])
        self.assertNotEqual(images_root, task_root / "resources" / "shared")


if __name__ == "__main__":
    unittest.main()
