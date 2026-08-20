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
            (ROOT / "contracts" / "v1" / "examples" / "plans" / "image-only.json").read_text()
        )
        self.registry.validate("task-plan", example)

    def test_registry_rejects_unknown_contract_version(self) -> None:
        with self.assertRaises(HarnessError) as captured:
            self.registry.validate("main-task", {"schema_version": "2.0"})
        self.assertEqual(captured.exception.code, "SCHEMA_VERSION_UNSUPPORTED")

    def test_registry_dispatches_task_card_minor_without_upgrading_other_objects(self) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "golden" / "image-task-card-mapping-v1.1.json").read_text()
        )
        self.registry.validate("task-card", fixture["harness_task_card"])
        self.registry.validate("task-card-v1.1", fixture["harness_task_card"])

        old_card = json.loads(
            (ROOT / "contracts" / "v1" / "examples" / "plans" / "image-only.json").read_text()
        )["task_cards"][0]
        self.registry.validate("task-card", old_card)

        with self.assertRaises(HarnessError) as unsupported:
            self.registry.validate("main-task", {"schema_version": "1.1"})
        self.assertEqual(unsupported.exception.code, "SCHEMA_VERSION_UNSUPPORTED")

    def test_registry_reports_a_stable_validation_error(self) -> None:
        with self.assertRaises(HarnessError) as captured:
            self.registry.validate("main-task", {})
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")
        self.assertEqual(captured.exception.details["schema"], "main-task.schema.json")
