from __future__ import annotations

import hashlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.adapters import PrepareRequest
from harness.adapters.image import ImageAgentAdapter, image_dependency_pythonpath_entries
from harness.adapters.image_delivery import normalize_image_delivery, stage_final_delivery
from harness.adapters.image_workflow import map_advance_payload
from harness.core.errors import HarnessError
from harness.services.agent_config_materialization import ImageAgentConfigMaterializer
from harness.services.assets import AssetService
from harness.services.task_config import TaskConfigService
from harness.storage.atomic import atomic_write_json, digest_json
from harness.storage.repository import utc_now
from PIL import Image
from runtime_helpers import (
    build_config_snapshot,
    build_service,
    create_task,
    envelope,
    instance,
    stage,
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE_SCHEMA = ROOT / "tests" / "fixtures" / "image-agent-contract" / "ImageTaskCard.schema.json"


class ImageAdapterTests(unittest.TestCase):
    def test_windows_target_dependencies_include_pywin32_pth_roots(self) -> None:
        artifact = Path("runtime") / "image-agent-artifact"

        self.assertEqual(
            image_dependency_pythonpath_entries(artifact, os_name="nt"),
            (
                artifact / "_dependencies",
                artifact / "_dependencies" / "win32",
                artifact / "_dependencies" / "win32" / "lib",
                artifact / "_dependencies" / "pythonwin",
            ),
        )
        self.assertEqual(
            image_dependency_pythonpath_entries(artifact, os_name="posix"),
            (artifact / "_dependencies",),
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        create_task(self.commands, "t_image_adapter")
        self.assets = AssetService(self.store)
        self.task_config = TaskConfigService(self.store, build_config_snapshot())
        self.image_config = ImageAgentConfigMaterializer(self.store, self.task_config)
        source_root = self.root / "image-source"
        (source_root / "schemas").mkdir(parents=True)
        shutil.copyfile(IMAGE_SCHEMA, source_root / "schemas" / "ImageTaskCard.schema.json")
        self.adapter = ImageAgentAdapter(
            self.store,
            self.store.contracts,
            self.assets,
            self.image_config,
            source_root=source_root,
            interpreter=Path(sys.executable),
            dependency_root=source_root,
        )

    def test_recovery_replays_the_idempotent_start_when_no_job_was_persisted(self) -> None:
        atomic_write_json(
            self.adapter._state_path("t_image_adapter", "i_image_adapter"),
            {"job_id": None},
        )
        snapshot = {
            **instance(
                "t_image_adapter", "i_image_adapter", "s_image", "image", True
            ),
            "status": "RUNNING",
        }

        with patch.object(self.adapter, "get_status") as get_status:
            recovery = self.adapter.recover(snapshot)

        self.assertFalse(recovery.recovered)
        self.assertEqual(recovery.status, "RUNNING")
        self.assertEqual(recovery.details["mode"], "idempotent_start_replay")
        get_status.assert_not_called()

    def test_prepare_exposes_only_managed_runtime_identity_and_capability(self) -> None:
        artifact = self.root / "runtime-artifact"
        artifact.mkdir()
        (artifact / "main_front.py").write_text("# test entrypoint\n", encoding="utf-8")
        self.adapter.runtime_artifact_root = artifact
        self.adapter.runtime_attestation = object()  # type: ignore[assignment]

        with (
            patch.object(self.adapter, "_validate_runtime_source"),
            patch.object(self.adapter.image_config, "materialize"),
            patch.object(
                self.adapter,
                "map_task_card",
                return_value={"task_id": "i_image_adapter", "project_id": "i_image_adapter"},
            ),
        ):
            spec = self.adapter.prepare(self._request(self._card([])))

        self.assertEqual(spec.public_environment["HARNESS_TASK_ID"], "t_image_adapter")
        self.assertEqual(spec.public_environment["HARNESS_INSTANCE_ID"], "i_image_adapter")
        self.assertEqual(spec.public_environment["INSTANCE_RUNTIME_SETTINGS_V2"], "1")
        self.assertNotIn("request_key", " ".join(spec.public_environment))

    def test_managed_runtime_apply_validates_and_forwards_the_frozen_receipt(self) -> None:
        card = self._card([])
        self.commands.save_plan(
            "t_image_adapter",
            stages=[
                stage(
                    "t_image_adapter",
                    "s_image",
                    "image",
                    1,
                    [],
                    True,
                    ["i_image_adapter"],
                )
            ],
            instances=[
                instance(
                    "t_image_adapter", "i_image_adapter", "s_image", "image", True
                )
            ],
            task_cards=[card],
            envelope=envelope(
                "save-managed-config-apply",
                self.store.task.revision("t_image_adapter", "t_image_adapter"),
            ),
        )
        control = (
            self.store.layout.initialize_instance("t_image_adapter", "i_image_adapter")
            / "runtime"
            / "managed-control.json"
        )
        atomic_write_json(
            control,
            {
                "instance_id": "i_image_adapter",
                "request_key": "managed-adapter-key-for-tests-12345",
            },
        )
        source = "checkpoint_0123456789abcdef01234567"
        target = "checkpoint_89abcdef0123456701234567"
        config_hash = "a" * 64
        response = {
            "status": "APPLIED_ON_BRANCH",
            "runtime_config_revision_id": "cfg-inst-r000002",
            "branch_id": "config_branch_1",
            "checkpoint_id": target,
            "from_checkpoint": source,
            "effective_from_state": "confirmation_build",
            "config_hash": config_hash,
        }
        with (
            patch.object(
                self.adapter, "_base_url", return_value="http://127.0.0.1:18101"
            ),
            patch.object(self.adapter, "_request", return_value=response) as request,
        ):
            receipt = self.adapter.apply_runtime_revision(
                "i_image_adapter",
                revision_id="cfg-inst-r000002",
                from_checkpoint=source,
                expected_config_hash=config_hash,
                expected_project_revision_id="cfg-inst-r000001",
                expected_project_config_hash="b" * 64,
                effective_from_state="confirmation_build",
                idempotency_key="managed-apply-command",
            )

        self.assertEqual(
            receipt,
            {
                "revision_id": "cfg-inst-r000002",
                "branch_id": "config_branch_1",
                "checkpoint_id": target,
                "from_checkpoint": source,
                "effective_from_state": "confirmation_build",
                "config_hash": config_hash,
            },
        )
        self.assertEqual(
            request.call_args.kwargs["headers"],
            {"X-Harness-Adapter-Key": "managed-adapter-key-for-tests-12345"},
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_task_card_maps_only_verified_assets_and_declared_fields(self) -> None:
        imported, selected = self._import_brief()
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
                "harness_output_contract": {
                    "expected_deliveries": [
                        {
                            "kind": "image",
                            "role": "final_artwork",
                            "required": True,
                            "accepted_mime_types": ["image/png"],
                        }
                    ],
                    "aspect_ratio": "16:9",
                },
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

    def test_prompt_only_task_card_maps_to_a_stable_auditable_source(self) -> None:
        card = self._card([])

        validation = self.adapter.validate_task_card(card)
        first = self.adapter.map_task_card(self._request(card))
        replay = self.adapter.map_task_card(self._request(card))

        self.assertTrue(validation.valid, validation.errors)
        self.assertEqual(replay, first)
        self.assertEqual(
            first["source_refs"],
            [
                {
                    "ref_id": card["card_id"],
                    "ref_type": "task_card",
                    "excerpt": card["objective"],
                    "source_hash": digest_json(
                        {
                            "objective": card["objective"],
                            "instructions": card["instructions"],
                            "parameters": card["parameters"],
                            "revision": card["revision"],
                        }
                    ),
                }
            ],
        )
        self.assertEqual(first["asset_inputs"], [])

    def test_asset_mapping_rejects_forged_manifest_paths_and_corrupt_bytes(self) -> None:
        imported, selected = self._import_brief()
        reference = selected["task_card_inputs"][0]
        forged = {
            **reference,
            "manifest_relpath": f"resources/manifests/{imported['asset_id']}.json",
        }

        with self.assertRaises(HarnessError) as forged_error:
            self.adapter.map_task_card(self._request(self._card([forged])))
        self.assertEqual(forged_error.exception.code, "VALIDATION_ERROR")

        manifest = self.assets.verify_asset("t_image_adapter", imported["asset_id"])
        asset_path = (
            self.store.layout.workspace_root
            / "tasks"
            / "t_image_adapter"
            / manifest["relative_path"]
        )
        asset_path.chmod(0o640)
        asset_path.write_bytes(b"corrupt asset bytes")

        with self.assertRaises(HarnessError) as corrupt_error:
            self.adapter.map_task_card(
                self._request(self._card(selected["task_card_inputs"]))
            )
        self.assertEqual(corrupt_error.exception.code, "ASSET_CORRUPTED")

    def test_ui_url_allowlist_binds_host_port_and_running_process(self) -> None:
        instance = {
            "process": {"state": "RUNNING", "port": 19001},
        }
        self.assertTrue(
            self.adapter.validate_ui_url(instance, "http://127.0.0.1:19001/").valid
        )
        for value in (
            "https://127.0.0.1:19001/",
            "http://public.example:19001/",
            "http://127.0.0.1:19002/",
            "http://127.0.0.1:19001/admin",
            "http://user@127.0.0.1:19001/",
        ):
            with self.subTest(value=value):
                self.assertFalse(self.adapter.validate_ui_url(instance, value).valid)

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
                "unknown_actions": [],
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
                "unknown_actions": [],
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
            {
                "manifest": {"failed_step": None},
                "snapshot": {},
                "capabilities": [],
                "unknown_actions": [],
            },
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

    def test_usage_collection_maps_tokens_and_image_units_with_stable_cursor(self) -> None:
        pages = [
            {
                "items": [
                    {
                        "sequence": 4,
                        "timestamp": "2026-08-21T10:00:00+00:00",
                        "usage_id": "usage_text",
                        "request_id": "local_text",
                        "provider_request_id": "provider_text",
                        "provider": "ark",
                        "model": "reasoner",
                        "call_type": "reasoning_llm",
                        "usage_basis": "tokens",
                        "token_usage": {
                            "input_tokens": 7,
                            "output_tokens": 3,
                            "cached_input_tokens": 1,
                            "reasoning_tokens": 2,
                            "total_tokens": 10,
                        },
                        "billing_units": [],
                        "raw_usage": {
                            "prompt_tokens": 7,
                            "completion_tokens": 3,
                            "total_tokens": 10,
                            "prompt_tokens_details": {"cached_tokens": 1},
                            "completion_tokens_details": {"reasoning_tokens": 2},
                        },
                    }
                ],
                "next_cursor": 4,
                "has_more": True,
            },
            {
                "items": [
                    {
                        "sequence": 9,
                        "timestamp": "2026-08-21T10:00:01Z",
                        "usage_id": "usage_image",
                        "request_id": "local_image",
                        "provider_request_id": None,
                        "provider": "ark",
                        "model": "seedream",
                        "call_type": "text_to_image_model",
                        "usage_basis": "image_units",
                        "token_usage": None,
                        "billing_units": [
                            {
                                "unit": "image",
                                "quantity": 1,
                                "attributes": {
                                    "resolution": "2560x1440",
                                    "model_tier": "seedream",
                                },
                            }
                        ],
                        "raw_usage": {},
                    }
                ],
                "next_cursor": 9,
                "has_more": False,
            },
        ]
        instance = {"instance_id": "i_image_adapter"}
        with (
            patch.object(self.adapter, "_task_id_for_instance", return_value="t_image_adapter"),
            patch.object(self.adapter, "_base_url", return_value="http://127.0.0.1:1"),
            patch.object(self.store.instance, "get", return_value=instance),
            patch.object(self.adapter, "_request", side_effect=pages) as request,
        ):
            events = self.adapter.collect_usage("i_image_adapter", None)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["schema_version"], "1.1")
        self.assertEqual(events[0]["total_tokens"], 10)
        self.assertEqual(
            events[0]["raw_usage"]["completion_tokens_details"],
            {"reasoning_tokens": 2},
        )
        self.assertEqual(events[0]["occurred_at"], "2026-08-21T10:00:00.000000Z")
        self.assertEqual(events[1]["usage_basis"], "image_units")
        self.assertEqual(events[1]["total_tokens"], 0)
        self.store.contracts.validate("token-usage-event", events[0])
        self.store.contracts.validate("token-usage-event", events[1])
        self.assertIn("after=4", request.call_args_list[1].args[2])

        with (
            patch.object(self.adapter, "_task_id_for_instance"),
            self.assertRaisesRegex(HarnessError, "usage cursor"),
        ):
            self.adapter.collect_usage("i_image_adapter", "provider_cursor")

    def test_usage_collection_rejects_non_metering_raw_provider_fields(self) -> None:
        page = {
            "items": [
                {
                    "sequence": 1,
                    "timestamp": "2026-08-21T10:00:00Z",
                    "usage_id": "usage_secret",
                    "request_id": "local_secret",
                    "provider_request_id": None,
                    "provider": "ark",
                    "model": "reasoner",
                    "call_type": "reasoning_llm",
                    "usage_basis": "tokens",
                    "token_usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 2,
                    },
                    "billing_units": [],
                    "raw_usage": {},
                }
            ],
            "next_cursor": 1,
            "has_more": False,
        }
        candidates = (
            {"api_key": "must-not-cross"},
            {"password": "plain-password-value"},
            {"private_key": "plain-private-key-value"},
            {"token": "plain-token-value"},
            {"note": "ordinary-but-not-a-metering-field"},
            {"prompt_tokens_details": {"password": "nested-value"}},
            {"prompt_tokens": True},
            {"prompt_tokens": -1},
            {"prompt_tokens_details": {"cached_tokens": "1"}},
        )
        for raw_usage in candidates:
            with self.subTest(raw_usage=raw_usage):
                page["items"][0]["raw_usage"] = raw_usage
                with (
                    patch.object(
                        self.adapter,
                        "_task_id_for_instance",
                        return_value="t_image_adapter",
                    ),
                    patch.object(
                        self.adapter, "_base_url", return_value="http://127.0.0.1:1"
                    ),
                    patch.object(
                        self.store.instance,
                        "get",
                        return_value={"instance_id": "i_image_adapter"},
                    ),
                    patch.object(self.adapter, "_request", return_value=page),
                    self.assertRaisesRegex(HarnessError, "raw usage"),
                ):
                    self.adapter.collect_usage("i_image_adapter", None)

    def test_usage_collection_rejects_a_nonadvancing_producer_page(self) -> None:
        with (
            patch.object(self.adapter, "_task_id_for_instance", return_value="t_image_adapter"),
            patch.object(self.adapter, "_base_url", return_value="http://127.0.0.1:1"),
            patch.object(
                self.store.instance,
                "get",
                return_value={"instance_id": "i_image_adapter"},
            ),
            patch.object(
                self.adapter,
                "_request",
                return_value={"items": [], "next_cursor": 0, "has_more": True},
            ),
            self.assertRaisesRegex(HarnessError, "did not advance"),
        ):
            self.adapter.collect_usage("i_image_adapter", None)

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
                "unknown_actions": [],
            },
            None,
            8,
            None,
        )
        self.assertEqual(observation.status, "WAITING_APPROVAL")
        self.assertEqual(observation.capabilities, ("review_calibration",))

    def test_collect_delivery_bundles_stages_image_and_markdown_together(self) -> None:
        card = self._card([])
        self.commands.save_plan(
            "t_image_adapter",
            stages=[
                stage(
                    "t_image_adapter",
                    "s_image",
                    "image",
                    1,
                    [],
                    True,
                    ["i_image_adapter"],
                )
            ],
            instances=[
                instance(
                    "t_image_adapter",
                    "i_image_adapter",
                    "s_image",
                    "image",
                    True,
                )
            ],
            task_cards=[card],
            envelope=envelope(
                "save-image-adapter-bundle-plan",
                self.store.task.revision("t_image_adapter", "t_image_adapter"),
            ),
        )
        instance_root = self.store.layout.initialize_instance(
            "t_image_adapter", "i_image_adapter"
        )
        delivery = instance_root / "work" / "i_image_adapter" / "delivery"
        delivery.mkdir(parents=True)
        image_buffer = io.BytesIO()
        Image.new("RGB", (48, 32), "navy").save(image_buffer, "PNG")
        image_bytes = image_buffer.getvalue()
        note_bytes = b"# Final branch note\n"
        (delivery / "bundle-image.png").write_bytes(image_bytes)
        (delivery / "bundle-note.md").write_bytes(note_bytes)
        response = {
            "schema_version": "1.0",
            "candidates": [
                {
                    "bundle_id": "bundle_adapter_main_01",
                    "branch_id": "main",
                    "checkpoint_id": "checkpoint_0123456789abcdef01234567",
                    "files": {
                        "image": "delivery/bundle-image.png",
                        "markdown": "delivery/bundle-note.md",
                        "json": "delivery/bundle.json",
                    },
                    "image": {
                        "sha256": hashlib.sha256(image_bytes).hexdigest(),
                    },
                    "design_note": {
                        "sha256": hashlib.sha256(note_bytes).hexdigest(),
                    },
                    "created_at": "2026-08-22T17:20:00Z",
                }
            ],
        }
        with (
            patch.object(
                self.adapter, "_base_url", return_value="http://127.0.0.1:1"
            ),
            patch.object(self.adapter, "_request", return_value=response),
        ):
            candidates = self.adapter.collect_delivery_bundles("i_image_adapter")

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["branch_id"], "main")
        self.assertEqual(candidate["image"]["mime_type"], "image/png")
        self.assertEqual(
            (candidate["image"]["width"], candidate["image"]["height"]),
            (48, 32),
        )
        self.assertEqual(candidate["design_note"]["mime_type"], "text/markdown")
        self.store.contracts.validate("delivery-bundle-candidate", candidate)
        for part in (candidate["image"], candidate["design_note"]):
            self.assertTrue(
                (
                    self.store.layout.workspace_root
                    / "tasks"
                    / "t_image_adapter"
                    / part["private_relative_path"]
                ).is_file()
            )

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

    def test_jpeg_delivery_is_deterministically_derived_as_png(self) -> None:
        source = self.root / "outputs" / "provider-final.jpg"
        source.parent.mkdir()
        Image.new("RGB", (32, 18), color=(10, 40, 90)).save(source, format="JPEG", quality=90)

        first = normalize_image_delivery(
            source,
            accepted_mime_types=("image/png",),
        )
        second = normalize_image_delivery(
            source,
            accepted_mime_types=("image/png",),
        )

        self.assertEqual(first["mime_type"], "image/png")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["path"], second["path"])
        self.assertEqual(first["derivation"]["source_mime_type"], "image/jpeg")
        self.assertEqual(first["derivation"]["derived_sha256"], first["sha256"])
        self.assertTrue(first["path"].read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_jpeg_dimensions_are_bounded_before_pixel_decode(self) -> None:
        source = self.root / "outputs" / "oversized.jpg"
        source.parent.mkdir()
        source.write_bytes(b"\xff\xd8\xffsynthetic")

        class OversizedImage:
            size = (8_000, 8_000)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def load(self):
                raise AssertionError("pixel decode must not run")

        with patch(
            "harness.adapters.image_delivery.Image.open",
            return_value=OversizedImage(),
        ), self.assertRaises(HarnessError) as rejected:
            normalize_image_delivery(source, accepted_mime_types=("image/png",))

        self.assertEqual(rejected.exception.code, "ASSET_VALIDATION_FAILED")

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

    def _import_brief(self) -> tuple[dict, dict]:
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
        return imported, selected

    def _request(self, card: dict) -> PrepareRequest:
        return PrepareRequest(
            instance={
                "task_id": "t_image_adapter",
                "instance_id": "i_image_adapter",
            },
            task_card=card,
            task_root=self.store.layout.workspace_root / "tasks" / "t_image_adapter",
            config_ref=self.root / "runtime.yaml",
        )


if __name__ == "__main__":
    unittest.main()
