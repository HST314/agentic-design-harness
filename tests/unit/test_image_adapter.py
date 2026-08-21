from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import PrepareRequest
from harness.adapters.image import ImageAgentAdapter
from harness.adapters.image_delivery import stage_final_delivery
from harness.adapters.image_workflow import map_advance_payload
from harness.core.errors import HarnessError
from harness.services.assets import AssetService
from harness.services.configuration import ConfigurationService
from harness.storage.atomic import digest_json
from harness.storage.repository import utc_now
from runtime_helpers import build_service, create_task

ROOT = Path(__file__).resolve().parents[2]
IMAGE_SCHEMA = ROOT / "tests" / "fixtures" / "image-agent-contract" / "ImageTaskCard.schema.json"


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
        shutil.copyfile(IMAGE_SCHEMA, source_root / "schemas" / "ImageTaskCard.schema.json")
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
                "harness_instructions": ["Use the registered brief as the only copy source."],
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
        self.assertIn("unknown capability", unknown_capability.details["compatibility_error"])

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

    def test_succeeded_job_without_snapshot_fails_closed(self) -> None:
        observation = self.adapter._observation(
            {"manifest": {"failed_step": None}, "snapshot": {}, "capabilities": []},
            {"job_id": "job_123", "status": "succeeded", "result": {}},
            8,
            None,
        )

        self.assertEqual(observation.status, "FAILED")
        self.assertIn("non-empty snapshot", observation.details["compatibility_error"])

    def test_snapshot_scalar_types_fail_closed(self) -> None:
        for field, malformed_value in (("waiting", "true"), ("completed", 1)):
            with self.subTest(field=field):
                observation = self.adapter._observation(
                    {
                        "manifest": {"failed_step": None},
                        "snapshot": {
                            "phase": "waiting_clarification",
                            "waiting": True,
                            field: malformed_value,
                        },
                        "capabilities": ["answer_clarification"],
                    },
                    None,
                    3,
                    None,
                )
                self.assertEqual(observation.status, "FAILED")
                self.assertIn("malformed snapshot", observation.details["compatibility_error"])

    def test_timeline_requires_strict_sequences_and_typed_pagination(self) -> None:
        duplicate_sequences = {
            "items": [
                {"sequence": 1, "type": "created", "timestamp": "2026-08-20T00:00:00Z"},
                {"sequence": 1, "type": "updated", "timestamp": "2026-08-20T00:00:01Z"},
            ],
            "next_cursor": 1,
            "has_more": False,
        }
        with self.assertRaisesRegex(HarnessError, "timeline cursor") as duplicate:
            self.adapter._validate_timeline(duplicate_sequences, previous=0)
        self.assertEqual(duplicate.exception.code, "VALIDATION_ERROR")

        with self.assertRaisesRegex(HarnessError, "timeline page") as pagination:
            self.adapter._validate_timeline(
                {"items": [], "next_cursor": 0, "has_more": "false"}, previous=0
            )
        self.assertEqual(pagination.exception.code, "VALIDATION_ERROR")

    def test_job_record_requires_typed_terminal_and_event_shapes(self) -> None:
        malformed_jobs = (
            {"job_id": "not-a-job", "status": "queued"},
            {"job_id": "job_123", "status": True},
            {"job_id": "job_123", "status": "succeeded", "result": []},
            {"job_id": "job_123", "status": "failed", "error": {}},
            {
                "job_id": "job_123",
                "status": "queued",
                "events": [{"seq": 1, "type": "queued", "timestamp": 1}],
            },
        )
        for job in malformed_jobs:
            with (
                self.subTest(job=job),
                self.assertRaisesRegex(HarnessError, "invalid job record") as rejected,
            ):
                self.adapter._require_job(job)
            self.assertEqual(rejected.exception.code, "VALIDATION_ERROR")

    def test_non_object_openapi_components_raise_stable_protocol_error(self) -> None:
        document = {"info": {"version": "1.0.0"}, "paths": {}, "components": []}
        with (
            patch.object(self.adapter, "_request", return_value=document),
            self.assertRaisesRegex(HarnessError, "OpenAPI metadata") as rejected,
        ):
            self.adapter._check_compatibility("http://127.0.0.1:1")
        self.assertEqual(rejected.exception.code, "VALIDATION_ERROR")

    def test_apply_config_hot_loads_policy_and_model_bindings(self) -> None:
        calls = []

        def request(_base_url, method, path, payload=None, **_kwargs):
            calls.append((method, path, payload))
            if path == "/api/settings/models" and method == "GET":
                return {
                    "library": {
                        "reasoning": [
                            {
                                "id": "model_target",
                                "provider": "fake",
                                "model": "target-model",
                            }
                        ]
                    },
                    "states": [
                        {
                            "state": "intake_clarify",
                            "group": "reasoning",
                            "binding": {
                                "provider": "fake",
                                "model": "old-model",
                            },
                        }
                    ],
                }
            return {"status": "ok"}

        runtime_files = {
            "runtime.yaml": {"offline_mode": True},
            "model-config.yaml": {
                "state_bindings": [
                    {
                        "state": "intake_clarify",
                        "provider": "fake",
                        "model": "target-model",
                    }
                ]
            },
        }
        state = {"schema_version": "1.0"}
        with (
            patch.object(self.adapter, "_task_id_for_instance", return_value="t_image_adapter"),
            patch.object(self.adapter, "_base_url", return_value="http://127.0.0.1:1"),
            patch.object(self.adapter, "_check_compatibility"),
            patch.object(self.configuration, "image_runtime_files", return_value=runtime_files),
            patch.object(self.adapter, "_request", side_effect=request),
            patch.object(self.adapter, "_state", return_value=state),
            patch.object(self.adapter, "_write_state") as write_state,
        ):
            result = self.adapter.apply_config(
                "i_image_adapter", {"config": {}}, 4, "config_apply_4"
            )

        self.assertTrue(result.accepted)
        self.assertEqual(result.details["updated_model_bindings"], ["intake_clarify"])
        self.assertIn(
            (
                "POST",
                "/api/settings/models",
                {
                    "bindings": {"intake_clarify": "model_target"},
                    "actor": "harness_config_service",
                    "confirmed": True,
                },
            ),
            calls,
        )
        self.assertEqual(state["config_revision"], 4)
        write_state.assert_called_once()

    def test_apply_config_preflights_models_before_mutating_policy(self) -> None:
        calls = []

        def request(_base_url, method, path, payload=None, **_kwargs):
            calls.append((method, path, payload))
            if path == "/api/settings/models" and method == "GET":
                return {
                    "library": {"reasoning": []},
                    "states": [
                        {
                            "state": "intake_clarify",
                            "group": "reasoning",
                            "binding": {
                                "provider": "other",
                                "model": "other-model",
                            },
                        }
                    ],
                }
            return {}

        with (
            patch.object(
                self.adapter,
                "_task_id_for_instance",
                return_value="t_image_adapter",
            ),
            patch.object(self.adapter, "_base_url", return_value="http://127.0.0.1:1"),
            patch.object(self.adapter, "_check_compatibility"),
            patch.object(self.adapter, "_request", side_effect=request),
            patch.object(
                self.adapter.configuration,
                "image_runtime_files",
                return_value={
                    "runtime.yaml": {"workspace_max_iterations": 8},
                    "model-config.yaml": {
                        "state_bindings": [
                            {
                                "state": "intake_clarify",
                                "provider": "fake",
                                "model": "missing-model",
                            }
                        ]
                    },
                },
            ),
        ):
            result = self.adapter.apply_config(
                "i_image_adapter", {"config": {}}, 5, "config_apply_5"
            )

        self.assertFalse(result.accepted)
        self.assertTrue(result.details["restart_required"])
        self.assertEqual(
            calls,
            [("GET", "/api/settings/models", None)],
        )

    def test_stop_cancels_only_an_active_image_job(self) -> None:
        state = {"job_id": "job_123"}
        responses = [
            {"job_id": "job_123", "status": "running"},
            {"job_id": "job_123", "status": "cancelling"},
        ]
        with (
            patch.object(self.adapter, "_task_id_for_instance", return_value="t_image_adapter"),
            patch.object(self.adapter, "_base_url", return_value="http://127.0.0.1:1"),
            patch.object(self.adapter, "_state", return_value=state),
            patch.object(self.adapter, "_request", side_effect=responses) as request,
            patch.object(self.adapter, "_write_state") as write_state,
        ):
            result = self.adapter.stop("i_image_adapter", "user_cancelled", "stop_image_adapter")

        self.assertTrue(result.accepted)
        self.assertEqual(request.call_args_list[1].args[1:3], ("POST", "/api/jobs/job_123/cancel"))
        self.assertEqual(state["job_status_at_stop"], "cancelling")
        write_state.assert_called_once()

    def test_harness_actions_map_to_strict_image_advance_requests(self) -> None:
        cases = (
            ("approve_taskbook", {"actor": "reviewer"}, {"task_approved": True}),
            ("approve_final", {"actor": "reviewer"}, {"final_approved": True}),
            (
                "select_master",
                {"selected_id": "candidate_one", "actor": "reviewer"},
                {"selected_id": "candidate_one"},
            ),
            (
                "answer_clarification",
                {"clarification_answers": {"q_one": "blue"}},
                {"clarification_answers": {"q_one": "blue"}},
            ),
            (
                "review_calibration",
                {"manual_action": "edit_and_execute", "edited_delta": "Increase contrast."},
                {"manual_action": "edit_and_execute", "edited_delta": "Increase contrast."},
            ),
            (
                "review_calibration",
                {"manual_action": "accept_current", "actor": "reviewer"},
                {"manual_action": "accept_current", "final_approved": True},
            ),
            (
                "submit_human_tune",
                {"human_prompt": "Move the title upward."},
                {"human_prompt": "Move the title upward."},
            ),
        )
        for action, payload, expected in cases:
            with self.subTest(action=action):
                mapped = map_advance_payload(action, payload)
                for key, value in expected.items():
                    self.assertEqual(mapped[key], value)

        with self.assertRaises(HarnessError) as unsupported:
            map_advance_payload("branch", {"actor": "reviewer"})
        self.assertEqual(unsupported.exception.code, "ADAPTER_UNAVAILABLE")
        with self.assertRaises(HarnessError) as extra:
            map_advance_payload("approve_final", {"actor": "reviewer", "selected_id": "unexpected"})
        self.assertEqual(extra.exception.code, "VALIDATION_ERROR")

    def test_waiting_projection_exposes_only_actions_supported_by_advance_request(self) -> None:
        observation = self.adapter._observation(
            {
                "manifest": {"failed_step": None},
                "snapshot": {
                    "phase": "waiting_human_approval",
                    "state": "self_check_iteration",
                    "waiting": True,
                },
                "capabilities": ["review_calibration", "enter_human_tune"],
            },
            None,
            8,
            None,
        )
        self.assertEqual(observation.status, "WAITING_APPROVAL")
        self.assertEqual(observation.capabilities, ("review_calibration",))

    def test_final_delivery_staging_rejects_symlink_and_digest_mismatch(self) -> None:
        project = self.root / "project"
        delivery = project / "delivery"
        delivery.mkdir(parents=True)
        source = delivery / "final.png"
        content = b"\x89PNG\r\n\x1a\nfinal"
        source.write_bytes(content)
        destination = self.root / "outputs" / "final.png"

        with self.assertRaises(HarnessError) as digest_mismatch:
            stage_final_delivery(
                project,
                Path("delivery/final.png"),
                destination,
                expected_sha256="0" * 64,
            )
        self.assertEqual(digest_mismatch.exception.code, "ASSET_CORRUPTED")
        self.assertFalse(destination.exists())

        source.unlink()
        source.symlink_to(self.root / "outside.png")
        with self.assertRaises(HarnessError) as symlink:
            stage_final_delivery(
                project,
                Path("delivery/final.png"),
                destination,
                expected_sha256="0" * 64,
            )
        self.assertEqual(symlink.exception.code, "ASSET_VALIDATION_FAILED")

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
