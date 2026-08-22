from __future__ import annotations

import json
import unittest
from pathlib import Path

from harness.contracts import ContractRegistry
from harness.core.errors import HarnessError

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
            "credential_pair_ref": "cred_contract",
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
