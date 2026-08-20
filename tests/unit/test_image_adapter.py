from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from harness.adapters import PrepareRequest
from harness.adapters.image import ImageAgentAdapter
from harness.core.errors import HarnessError
from harness.services.assets import AssetService
from harness.services.configuration import ConfigurationService
from harness.storage.atomic import digest_json
from harness.storage.repository import utc_now
from runtime_helpers import build_service, create_task

ROOT = Path(__file__).resolve().parents[2]
IMAGE_SCHEMA = (
    ROOT / "tests" / "fixtures" / "image-agent-contract" / "ImageTaskCard.schema.json"
)


class ImageAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        create_task(self.commands, "t_image_adapter")
        self.assets = AssetService(self.store)
        self.configuration = ConfigurationService(self.store)
        self.configuration.initialize()
        source_root = self.root / "image-source"
        (source_root / "schemas").mkdir(parents=True)
        shutil.copyfile(
            IMAGE_SCHEMA, source_root / "schemas" / "ImageTaskCard.schema.json"
        )
        self.adapter = ImageAgentAdapter(
            self.store,
            self.store.contracts,
            self.assets,
            self.configuration,
            source_root=source_root,
            interpreter=Path(sys.executable),
            dependency_root=source_root,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_task_card_maps_only_verified_assets_and_declared_fields(self) -> None:
        imported = self.assets.import_bytes(
            "t_image_adapter",
            filename="brief.md",
            content=b"# Approved campaign brief\n",
            description="Approved campaign copy and visual constraints.",
            source="user_upload",
            idempotency_key="import-image-adapter-brief",
        )
        selected = self.assets.select_inputs(
            "t_image_adapter", [imported["asset_id"]], manifest_id="image-adapter-inputs"
        )
        card = self._card(selected["task_card_inputs"])
        mapped = self.adapter.map_task_card(self._request(card))
        manifest = self.assets.verify_asset("t_image_adapter", imported["asset_id"])

        self.assertEqual(mapped["task_id"], "i_image_adapter")
        self.assertEqual(mapped["project_id"], "i_image_adapter")
        self.assertEqual(mapped["parent_task_id"], "t_image_adapter")
        self.assertEqual(mapped["usage_context"], "Internal campaign review.")
        self.assertEqual(
            mapped["known_facts"],
            {
                "harness_instructions": [
                    "Use the registered brief as the only copy source."
                ],
                "harness_parameters": {"aspect_ratio": "16:9", "variants": 2},
            },
        )
        self.assertEqual(
            mapped["source_refs"],
            [
                {
                    "ref_id": imported["asset_id"],
                    "ref_type": "document",
                    "excerpt": "Approved campaign copy and visual constraints.",
                    "source_hash": manifest["sha256"],
                }
            ],
        )
        self.assertEqual(mapped["asset_inputs"][0]["verified"], True)
        self.assertNotIn("schema_version", mapped)
        self.assertNotIn("credential_pair_ref", digest_json(mapped))

    def test_unknown_phase_or_capability_fails_closed(self) -> None:
        view = {
            "manifest": {"failed_step": None},
            "snapshot": {"phase": "future_phase"},
            "capabilities": [],
        }
        unknown_phase = self.adapter._observation(view, None, 1, None)
        self.assertEqual(unknown_phase.status, "FAILED")
        self.assertIn("unknown phase", unknown_phase.details["compatibility_error"])

        view["snapshot"]["phase"] = "waiting_clarification"
        view["capabilities"] = ["future_action"]
        unknown_capability = self.adapter._observation(view, None, 2, None)
        self.assertEqual(unknown_capability.status, "FAILED")
        self.assertIn(
            "unknown capability", unknown_capability.details["compatibility_error"]
        )

    def test_waiting_snapshot_maps_to_frozen_approval_observation(self) -> None:
        observation = self.adapter._observation(
            {
                "manifest": {"failed_step": None},
                "snapshot": {"phase": "waiting_clarification", "waiting": True},
                "capabilities": ["answer_clarification"],
            },
            {"job_id": "job_123", "status": "succeeded"},
            7,
            {"api_version": "1.0.0"},
        )
        self.assertEqual(observation.status, "WAITING_APPROVAL")
        self.assertEqual(observation.step_id, "waiting_clarification")
        self.assertEqual(observation.capabilities, ("answer_clarification",))
        self.assertEqual(observation.details["timeline_cursor"], 7)

    def test_waiting_flag_and_timeline_cursor_fail_closed(self) -> None:
        missing_waiting = self.adapter._observation(
            {
                "manifest": {"failed_step": None},
                "snapshot": {"phase": "waiting_clarification", "waiting": False},
                "capabilities": ["answer_clarification"],
            },
            {"job_id": "job_123", "status": "succeeded"},
            4,
            None,
        )
        self.assertEqual(missing_waiting.status, "FAILED")
        self.assertIn("waiting flag", missing_waiting.details["compatibility_error"])

        with self.assertRaisesRegex(HarnessError, "timeline cursor"):
            self.adapter._validate_timeline(
                {"items": [], "next_cursor": 5, "has_more": False}, previous=4
            )

    @staticmethod
    def _card(input_assets: list[dict[str, str]]) -> dict:
        return {
            "schema_version": "1.1",
            "card_id": "card_image_adapter",
            "revision": 1,
            "task_id": "t_image_adapter",
            "stage_id": "s_image",
            "instance_id": "i_image_adapter",
            "agent_type": "image",
            "objective": "Create a launch poster.",
            "instructions": ["Use the registered brief as the only copy source."],
            "input_assets": input_assets,
            "expected_deliveries": [
                {
                    "kind": "image",
                    "role": "final_artwork",
                    "required": True,
                    "accepted_mime_types": ["image/png"],
                }
            ],
            "parameters": {
                "aspect_ratio": "16:9",
                "variants": 2,
                "usage_context": "Internal campaign review.",
                "category_id": "generic_visual_delivery",
                "category_version": "1.0",
            },
            "created_at": utc_now(),
        }

    def _request(self, card: dict) -> PrepareRequest:
        return PrepareRequest(
            instance={
                "task_id": "t_image_adapter",
                "instance_id": "i_image_adapter",
            },
            task_card=card,
            task_root=self.store.layout.workspace_root / "tasks" / "t_image_adapter",
            config_ref=self.root / "runtime.yaml",
            credential_ref=("cred_test_01", 1),
        )


if __name__ == "__main__":
    unittest.main()
