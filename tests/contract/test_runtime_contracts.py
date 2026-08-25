from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness.contracts import ContractRegistry
from harness.core.errors import HarnessError
from harness.storage.atomic import digest_json

ROOT = Path(__file__).resolve().parents[2]


class RuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.registry = ContractRegistry(ROOT / "contracts" / "v1")

    def test_registry_validates_frozen_example(self) -> None:
        example = json.loads(
            (ROOT / "contracts" / "v1" / "examples" / "plans" / "image-only.json").read_text(
                encoding="utf-8"
            )
        )
        self.registry.validate("task-plan", example)

    def test_registry_rejects_unknown_contract_version(self) -> None:
        with self.assertRaises(HarnessError) as captured:
            self.registry.validate("main-task", {"schema_version": "2.0"})
        self.assertEqual(captured.exception.code, "SCHEMA_VERSION_UNSUPPORTED")

    def test_registry_dispatches_task_card_minor_without_upgrading_other_objects(self) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "golden" / "image-task-card-mapping-v1.1.json").read_text(
                encoding="utf-8"
            )
        )
        self.registry.validate("task-card", fixture["harness_task_card"])
        self.registry.validate("task-card-v1.1", fixture["harness_task_card"])

        old_card = json.loads(
            (ROOT / "contracts" / "v1" / "examples" / "plans" / "image-only.json").read_text(
                encoding="utf-8"
            )
        )["task_cards"][0]
        self.registry.validate("task-card", old_card)

        with self.assertRaises(HarnessError) as unsupported:
            self.registry.validate("main-task", {"schema_version": "1.1"})
        self.assertEqual(unsupported.exception.code, "SCHEMA_VERSION_UNSUPPORTED")

    def test_registry_dispatches_token_usage_event_1_1(self) -> None:
        event = {
            "schema_version": "1.1",
            "event_id": "usage_contract",
            "task_id": "t_contract",
            "instance_id": "i_contract",
            "agent_type": "image",
            "request_id": "request_contract",
            "provider_request_id": None,
            "provider": "ark",
            "model": "seedream",
            "call_type": "text_to_image_model",
            "usage_basis": "image_units",
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "billing_units": [
                {
                    "unit": "image",
                    "quantity": 1,
                    "attributes": {"resolution": "2560x1440"},
                }
            ],
            "raw_usage": {},
            "occurred_at": "2026-08-21T10:00:00Z",
        }

        self.registry.validate("token-usage-event", event)
        self.registry.validate("token-usage-event-v1.1", event)

    def test_registry_reports_a_stable_validation_error(self) -> None:
        with self.assertRaises(HarnessError) as captured:
            self.registry.validate("main-task", {})
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")
        self.assertEqual(captured.exception.details["schema"], "main-task.schema.json")

    def test_registry_dispatches_versioned_configuration_revision_contracts(self) -> None:
        created_at = "2026-08-25T03:00:00Z"
        task_body = {
            "provider_ids": ["ark"],
            "model_list": {"schema_version": "1.0", "models": []},
            "runtime": {"schema_version": "1.0", "image_agent": {}},
        }
        task_revision = {
            "schema_version": "2.0",
            "task_id": "task_contract",
            "revision_id": "task-config-r000001",
            "parent_revision_id": None,
            "source_system_revision": "cfg_contract",
            **task_body,
            "config_hash": digest_json(task_body),
            "created_by": {"type": "system", "id": "contract_test"},
            "created_at": created_at,
        }
        task_state = {
            "schema_version": "2.0",
            "task_id": "task_contract",
            "current_revision_id": "task-config-r000001",
            "source_system_revision": "cfg_contract",
            "locked_at": None,
            "locked_reason": None,
            "revision": 1,
            "created_at": created_at,
            "updated_at": created_at,
        }
        self.registry.validate("task-config-revision", task_revision)
        self.registry.validate("task-config-state", task_state)

        effective_runtime = {
            "question_preference": "proactive",
            "max_auto_questions": 3,
            "clarification_total_budget": 10,
            "candidate_concurrency": 5,
            "default_output_size": "2560x1440",
            "response_format": "url",
            "watermark": False,
            "self_check": {
                "termination": "solo",
                "fixed_rounds": 2,
                "max_rounds": 4,
                "stop_early_on_pass": False,
            },
        }
        manifest = {
            "schema_version": "2.0",
            "task_id": "task_contract",
            "instance_id": "instance_contract",
            "revision_id": "cfg-inst-r000001",
            "parent_revision_id": None,
            "task_config_revision_id": "task-config-r000001",
            "overrides": {},
            "effective_runtime": effective_runtime,
            "model_bindings": {
                "intake_clarify": "text-model",
                "confirmation_build": "text-model",
                "initial_candidate_generation": "image-model",
                "self_check_inspection": "vision-model",
                "self_check_rework": "image-model",
                "human_prompt_rework": "image-model",
            },
            "runtime_sha256": "a" * 64,
            "model_config_sha256": "b" * 64,
            "config_hash": "c" * 64,
            "created_by": {"type": "system", "id": "contract_test"},
            "created_at": created_at,
            "confirmed_at": created_at,
            "apply_mode": "before_start",
            "apply_status": "APPLIED",
            "branch_id": None,
            "checkpoint_id": None,
            "effective_from_state": "initial",
        }
        state = {
            "schema_version": "2.0",
            "task_id": "task_contract",
            "instance_id": "instance_contract",
            "current_revision_id": "cfg-inst-r000001",
            "pending_revision_id": None,
            "revision": 1,
            "created_at": created_at,
            "updated_at": created_at,
        }
        self.registry.validate("instance-runtime-config-manifest", manifest)
        self.registry.validate("instance-runtime-config-state", state)

    def test_runtime_revision_contract_rejects_unregistered_override_fields(self) -> None:
        manifest = self._valid_manifest()
        manifest["overrides"] = {"offline_mode": True}
        with self.assertRaises(HarnessError) as captured:
            self.registry.validate("instance-runtime-config-manifest", manifest)
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")

    @staticmethod
    def _valid_manifest() -> dict[str, object]:
        return {
            "schema_version": "2.0",
            "task_id": "task_contract",
            "instance_id": "instance_contract",
            "revision_id": "cfg-inst-r000001",
            "parent_revision_id": None,
            "task_config_revision_id": "task-config-r000001",
            "overrides": {},
            "effective_runtime": {
                "question_preference": "proactive",
                "max_auto_questions": 3,
                "clarification_total_budget": 10,
                "candidate_concurrency": 5,
                "default_output_size": "2560x1440",
                "response_format": "url",
                "watermark": False,
                "self_check": {
                    "termination": "solo",
                    "fixed_rounds": 2,
                    "max_rounds": 4,
                    "stop_early_on_pass": False,
                },
            },
            "model_bindings": {state: "model" for state in (
                "intake_clarify",
                "confirmation_build",
                "initial_candidate_generation",
                "self_check_inspection",
                "self_check_rework",
                "human_prompt_rework",
            )},
            "runtime_sha256": "a" * 64,
            "model_config_sha256": "b" * 64,
            "config_hash": "c" * 64,
            "created_by": {"type": "system", "id": "contract_test"},
            "created_at": "2026-08-25T03:00:00Z",
            "confirmed_at": "2026-08-25T03:00:00Z",
            "apply_mode": "before_start",
            "apply_status": "APPLIED",
            "branch_id": None,
            "checkpoint_id": None,
            "effective_from_state": "initial",
        }
