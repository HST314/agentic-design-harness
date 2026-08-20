from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness.adapters import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    AdapterRegistry,
    PptAgentContractAdapter,
    ValidationResult,
)
from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.application import HarnessApplicationService
from harness.services.assets import AssetService
from harness.services.configuration import ConfigurationService
from harness.services.credentials import CredentialPoolService
from harness.services.supervisor import ProcessSupervisor
from harness.storage.ndjson import recover_records
from runtime_helpers import build_service, create_task, envelope, image_plan, ppt_plan

CREDENTIAL_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "p1" / "credential-pairs.json"
)


class FakeImageAdapter:
    agent_type = "image"
    available = True

    def validate_task_card(self, card):
        errors = () if card.get("agent_type") == "image" else ("wrong agent type",)
        return ValidationResult(valid=not errors, errors=errors)

    def prepare(self, request):
        raise AssertionError("process preparation is outside this integration test")

    def start(self, instance_id, operation_id):
        return AdapterCommandResult(True, operation_id)

    def get_status(self, instance_id):
        return AdapterObservation("RUNNING")

    def request_advance(self, instance_id, action, payload, operation_id):
        return AdapterCommandResult(True, operation_id)

    def apply_config(self, instance_id, config, revision, operation_id):
        return AdapterCommandResult(True, operation_id)

    def collect_deliveries(self, instance_id):
        return []

    def collect_usage(self, instance_id, cursor):
        return []

    def get_ui_url(self, instance_id):
        return None

    def recover(self, instance_snapshot):
        return AdapterRecoveryResult(True, "RUNNING")


class HarnessApplicationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        self.credentials = CredentialPoolService(self.store)
        pairs = json.loads(CREDENTIAL_FIXTURE.read_text(encoding="utf-8"))["pairs"]
        self.credentials.configure_pool(pairs)
        self.assets = AssetService(self.store)
        self.configuration = ConfigurationService(self.store)
        self.configuration.initialize()
        self.supervisor = ProcessSupervisor(
            self.store, self.commands, self.credentials, self.configuration
        )
        self.adapters = AdapterRegistry(
            [FakeImageAdapter(), PptAgentContractAdapter()]
        )
        self.application = HarnessApplicationService(
            self.store,
            self.commands,
            self.assets,
            self.credentials,
            self.supervisor,
            self.adapters,
        )

    def tearDown(self) -> None:
        self.supervisor.close()
        self.store.close()
        self.temporary.cleanup()

    def test_plan_and_instance_creation_is_atomic_to_callers_and_idempotent(self) -> None:
        created = create_task(self.commands, "t_application")
        draft = image_plan("t_application", 2)
        first_card = draft["task_cards"][0]
        first_card["schema_version"] = "1.1"
        first_card["parameters"]["usage_context"] = "Internal review"
        result = self.application.save_plan_and_create_instances(
            "t_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake", "i_image_2": "fake"},
            operation_id="save_application_plan",
            envelope=envelope("save-application-plan", created["revision"]),
        )
        replay = self.application.save_plan_and_create_instances(
            "t_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake", "i_image_2": "fake"},
            operation_id="save_application_plan",
            envelope=envelope("save-application-plan", created["revision"]),
        )
        self.assertEqual(replay, result)
        self.assertEqual(
            [item["credential_pair_ref"] for item in result["plan"]["instances"]],
            ["cred_test_01", "cred_test_02"],
        )
        self.assertEqual(result["plan"]["task_cards"][0]["schema_version"], "1.1")
        assignments = [
            item
            for item in recover_records(self.credentials.events_path)
            if item["event_type"] == "CREDENTIAL_PAIR_ASSIGNED"
        ]
        self.assertEqual(len(assignments), 2)

    def test_crash_after_assignment_recovers_without_orphaning_the_plan(self) -> None:
        created = create_task(self.commands, "t_recover_application")
        draft = image_plan("t_recover_application", 2)

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_instance_created:i_image_1":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.save_plan_and_create_instances(
                "t_recover_application",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                providers={"i_image_1": "fake", "i_image_2": "fake"},
                operation_id="recover_application_plan",
                envelope=envelope("recover-application-plan", created["revision"]),
                crash_hook=crash,
            )
        self.assertIsNone(self.store.plan.get("t_recover_application", "t_recover_application"))
        recovered = self.application.recover()
        self.assertEqual(recovered[0]["status"], "RECOVERED")
        self.assertIsNotNone(
            self.store.plan.get("t_recover_application", "t_recover_application")
        )
        assignments = [
            item
            for item in recover_records(self.credentials.events_path)
            if item["event_type"] == "CREDENTIAL_PAIR_ASSIGNED"
        ]
        self.assertEqual(len(assignments), 2)

    def test_invalid_plan_is_rejected_before_any_credential_assignment(self) -> None:
        created = create_task(self.commands, "t_invalid_application")
        draft = image_plan("t_invalid_application", 2)
        draft["stages"][0]["instance_ids"] = ["i_image_1"]
        with self.assertRaises(HarnessError) as invalid:
            self.application.save_plan_and_create_instances(
                "t_invalid_application",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                providers={"i_image_1": "fake", "i_image_2": "fake"},
                operation_id="invalid_application_plan",
                envelope=envelope("invalid-application-plan", created["revision"]),
            )
        self.assertEqual(invalid.exception.code, "VALIDATION_ERROR")
        self.assertEqual(recover_records(self.credentials.events_path), [])
        self.assertFalse(self.application._intent_path("invalid_application_plan").exists())

    def test_unavailable_ppt_plan_saves_without_consuming_credentials(self) -> None:
        created = create_task(self.commands, "t_ppt_application")
        draft = ppt_plan("t_ppt_application")
        result = self.application.save_plan_and_create_instances(
            "t_ppt_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={},
            operation_id="save_ppt_application_plan",
            envelope=envelope("save-ppt-application-plan", created["revision"]),
        )
        self.assertEqual(result["plan"]["instances"][0]["status"], "UNAVAILABLE")
        self.assertEqual(recover_records(self.credentials.events_path), [])

    def test_concurrent_plan_operations_cannot_leave_losing_assignments(self) -> None:
        created = create_task(self.commands, "t_concurrent_application")
        draft = image_plan("t_concurrent_application", 2)

        def save(index: int):
            try:
                return self.application.save_plan_and_create_instances(
                    "t_concurrent_application",
                    stages=draft["stages"],
                    instances=draft["instances"],
                    task_cards=draft["task_cards"],
                    providers={"i_image_1": "fake", "i_image_2": "fake"},
                    operation_id=f"concurrent_application_{index}",
                    envelope=envelope(
                        f"concurrent-application-{index}", created["revision"]
                    ),
                )
            except HarnessError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(save, (1, 2)))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, HarnessError) for item in outcomes), 1)
        assignments = [
            item
            for item in recover_records(self.credentials.events_path)
            if item["event_type"] == "CREDENTIAL_PAIR_ASSIGNED"
        ]
        self.assertEqual(len(assignments), 2)


if __name__ == "__main__":
    unittest.main()
