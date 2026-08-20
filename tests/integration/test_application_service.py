from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import time
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
from harness.services.approvals import ApprovalInboxService
from harness.services.assets import AssetService
from harness.services.configuration import ConfigurationService
from harness.services.credentials import CredentialPoolService
from harness.services.process_runtime import AgentRuntimeArtifact, ProcessSpec
from harness.services.supervisor import ProcessSupervisor
from harness.storage.atomic import read_json
from harness.storage.ndjson import recover_records
from runtime_helpers import build_service, create_task, envelope, image_plan, ppt_plan

CREDENTIAL_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "p1" / "credential-pairs.json"
)
FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agent_process.py"


class FakeImageAdapter:
    agent_type = "image"
    available = True

    def __init__(self) -> None:
        self.runtime_spec = None
        self.start_calls = []
        self.advance_calls = []
        self.observation = AdapterObservation("RUNNING")
        self.deliveries = []
        self.advance_delay = 0.0

    def validate_task_card(self, card):
        errors = () if card.get("agent_type") == "image" else ("wrong agent type",)
        return ValidationResult(valid=not errors, errors=errors)

    def prepare(self, request):
        if self.runtime_spec is None:
            raise AssertionError("no runtime artifact was configured for this test")
        return self.runtime_spec

    def start(self, instance_id, operation_id):
        self.start_calls.append((instance_id, operation_id))
        return AdapterCommandResult(True, operation_id)

    def get_status(self, instance_id):
        return self.observation

    def request_advance(self, instance_id, action, payload, operation_id):
        if self.advance_delay:
            time.sleep(self.advance_delay)
        self.advance_calls.append((instance_id, action, payload, operation_id))
        return AdapterCommandResult(True, operation_id)

    def apply_config(self, instance_id, config, revision, operation_id):
        return AdapterCommandResult(True, operation_id)

    def collect_deliveries(self, instance_id):
        return self.deliveries

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
        self.approvals = ApprovalInboxService(self.store)
        self.configuration = ConfigurationService(self.store)
        self.configuration.initialize()
        self.supervisor = ProcessSupervisor(
            self.store, self.commands, self.credentials, self.configuration
        )
        self.fake_adapter = FakeImageAdapter()
        self.adapters = AdapterRegistry(
            [self.fake_adapter, PptAgentContractAdapter()]
        )
        self.application = HarnessApplicationService(
            self.store,
            self.commands,
            self.assets,
            self.approvals,
            self.credentials,
            self.supervisor,
            self.adapters,
        )
        self.read_only_artifacts: list[Path] = []

    def tearDown(self) -> None:
        self.supervisor.close()
        self.store.close()
        for root in self.read_only_artifacts:
            root.chmod(0o755)
            for path in root.rglob("*"):
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() else 0o644)
        self.temporary.cleanup()

    def _configure_runtime_artifact(self, name: str) -> None:
        artifact_root = self.root / name
        artifact_root.mkdir()
        entrypoint = artifact_root / "fake_agent_process.py"
        shutil.copyfile(FAKE_AGENT, entrypoint)
        (artifact_root / "requirements.lock").write_text(
            "stdlib-only\n", encoding="utf-8"
        )
        for path in artifact_root.rglob("*"):
            path.chmod(0o444)
        artifact_root.chmod(0o555)
        self.read_only_artifacts.append(artifact_root)
        self.fake_adapter.runtime_spec = ProcessSpec(
            command=(sys.executable, str(entrypoint)),
            runtime_artifact=AgentRuntimeArtifact(
                artifact_id=name,
                revision="1",
                source_root=artifact_root,
                entrypoint_relpath="fake_agent_process.py",
                dependency_lock_relpaths=("requirements.lock",),
            ),
        )

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

    def test_stale_revision_is_rejected_before_intent_or_instance_creation(self) -> None:
        created = create_task(self.commands, "t_stale_application")
        self.commands.register_input_manifest(
            "t_stale_application",
            "assets/input-v2.json",
            envelope("advance-stale-application", created["revision"]),
        )
        draft = image_plan("t_stale_application")

        with self.assertRaises(HarnessError) as stale:
            self.application.save_plan_and_create_instances(
                "t_stale_application",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                providers={"i_image_1": "fake"},
                operation_id="stale_application_plan",
                envelope=envelope("stale-application-plan", created["revision"]),
            )

        self.assertEqual(stale.exception.code, "REVISION_CONFLICT")
        self.assertEqual(recover_records(self.credentials.events_path), [])
        self.assertIsNone(self.store.instance.get("t_stale_application", "i_image_1"))
        self.assertFalse(self.application._intent_path("stale_application_plan").exists())
        self.assertEqual(self.application.recover(), [])

    def test_revision_advance_after_intent_aborts_before_first_assignment(self) -> None:
        created = create_task(self.commands, "t_intent_revision_advance")
        draft = image_plan("t_intent_revision_advance", 2)

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_application_intent":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.save_plan_and_create_instances(
                "t_intent_revision_advance",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                providers={"i_image_1": "fake", "i_image_2": "fake"},
                operation_id="intent_revision_advance",
                envelope=envelope("intent-revision-advance", created["revision"]),
                crash_hook=crash,
            )
        self.commands.register_input_manifest(
            "t_intent_revision_advance",
            "assets/revised-input.json",
            envelope("advance-after-intent", created["revision"]),
        )

        recovered = self.application.recover()

        self.assertEqual(
            recovered,
            [
                {
                    "operation_id": "intent_revision_advance",
                    "status": "ABORTED",
                    "error_code": "REVISION_CONFLICT",
                }
            ],
        )
        intent = read_json(self.application._intent_path("intent_revision_advance"))
        self.assertEqual(intent["state"], "ABORTED")
        self.assertEqual(recover_records(self.credentials.events_path), [])
        self.assertIsNone(
            self.store.instance.get("t_intent_revision_advance", "i_image_1")
        )
        self.assertIsNone(
            self.store.instance.get("t_intent_revision_advance", "i_image_2")
        )
        self.assertEqual(self.application.recover(), [])

    def test_revision_advance_after_partial_assignment_compensates_and_aborts(self) -> None:
        created = create_task(self.commands, "t_partial_revision_advance")
        draft = image_plan("t_partial_revision_advance", 2)

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_instance_created:i_image_1":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.save_plan_and_create_instances(
                "t_partial_revision_advance",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                providers={"i_image_1": "fake", "i_image_2": "fake"},
                operation_id="partial_revision_advance",
                envelope=envelope("partial-revision-advance", created["revision"]),
                crash_hook=crash,
            )
        self.assertIsNotNone(
            self.store.instance.get("t_partial_revision_advance", "i_image_1")
        )
        advanced = self.commands.register_input_manifest(
            "t_partial_revision_advance",
            "assets/revised-input.json",
            envelope("advance-after-partial", created["revision"]),
        )

        recovered = self.application.recover()

        self.assertEqual(
            recovered,
            [
                {
                    "operation_id": "partial_revision_advance",
                    "status": "ABORTED",
                    "error_code": "REVISION_CONFLICT",
                }
            ],
        )
        intent = read_json(self.application._intent_path("partial_revision_advance"))
        self.assertEqual(intent["state"], "ABORTED")
        events = recover_records(self.credentials.events_path)
        self.assertEqual(
            [item["event_type"] for item in events],
            ["CREDENTIAL_PAIR_ASSIGNED", "CREDENTIAL_INSTANCE_CREATION_REVOKED"],
        )
        credential_state = read_json(self.credentials.state_path)
        self.assertEqual(credential_state["assignments"], {})
        for instance_id in ("i_image_1", "i_image_2"):
            self.assertIsNone(
                self.store.instance.get("t_partial_revision_advance", instance_id)
            )

        # Credential recovery must retire the compensated snapshot again if the
        # generic event-first store rebuild recreates it during a later restart.
        self.store.recover()
        self.credentials.recover()
        self.assertIsNone(
            self.store.instance.get("t_partial_revision_advance", "i_image_1")
        )
        self.assertEqual(self.application.recover(), [])

        def crash_retry(checkpoint: str) -> None:
            if checkpoint == "after_instance_created:i_image_1":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.save_plan_and_create_instances(
                "t_partial_revision_advance",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                providers={"i_image_1": "fake", "i_image_2": "fake"},
                operation_id="partial_revision_retry",
                envelope=envelope("partial-revision-retry", advanced["revision"]),
                crash_hook=crash_retry,
            )
        self.store.recover()
        self.credentials.recover()
        retry_recovery = self.application.recover()
        self.assertEqual(retry_recovery[0]["status"], "RECOVERED")
        retry_plan = self.store.plan.get(
            "t_partial_revision_advance", "t_partial_revision_advance"
        )
        self.assertEqual(retry_plan["task"]["status"], "AWAITING_START_CONFIRMATION")
        self.assertEqual(
            len(retry_plan["instances"]),
            2,
        )

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
        self.assertEqual(
            result["plan"]["instances"][0]["credential_pair_ref"],
            "ppt_adapter_unavailable",
        )
        self.assertEqual(recover_records(self.credentials.events_path), [])

    def test_start_intent_replays_adapter_start_after_process_crash_window(self) -> None:
        self._configure_runtime_artifact("application-fake-agent")
        created = create_task(self.commands, "t_start_application")
        draft = image_plan("t_start_application")
        saved = self.application.save_plan_and_create_instances(
            "t_start_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake"},
            operation_id="prepare_start_application",
            envelope=envelope("prepare-start-application", created["revision"]),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_process_started:i_image_1":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.confirm_and_start_ready_instances(
                "t_start_application",
                operation_id="start_application_instances",
                envelope=envelope(
                    "start-application-instances", saved["task_revision"]
                ),
                crash_hook=crash,
            )
        self.assertEqual(self.fake_adapter.start_calls, [])
        recovered = self.application.recover()
        self.assertEqual(recovered[0]["status"], "RECOVERED")
        self.assertEqual(len(self.fake_adapter.start_calls), 1)
        replay = self.application.confirm_and_start_ready_instances(
            "t_start_application",
            operation_id="start_application_instances",
            envelope=envelope("start-application-instances", saved["task_revision"]),
        )
        self.assertEqual(len(replay["launches"]), 1)
        self.assertEqual(len(self.fake_adapter.start_calls), 1)
        self.application.cancel_instance("t_start_application", "i_image_1")

    def test_start_intent_is_durable_before_manual_confirmation(self) -> None:
        self._configure_runtime_artifact("pre-confirmation-fake-agent")
        created = create_task(self.commands, "t_confirm_recovery")
        draft = image_plan("t_confirm_recovery")
        saved = self.application.save_plan_and_create_instances(
            "t_confirm_recovery",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake"},
            operation_id="prepare_confirm_recovery",
            envelope=envelope("prepare-confirm-recovery", created["revision"]),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_start_intent":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.confirm_and_start_ready_instances(
                "t_confirm_recovery",
                operation_id="start_confirm_recovery",
                envelope=envelope("start-confirm-recovery", saved["task_revision"]),
                crash_hook=crash,
            )
        plan = self.store.plan.get("t_confirm_recovery", "t_confirm_recovery")
        self.assertEqual(plan["task"]["status"], "AWAITING_START_CONFIRMATION")
        recovered = self.application.recover()
        self.assertEqual(recovered[0]["status"], "RECOVERED")
        self.assertEqual(len(self.fake_adapter.start_calls), 1)
        self.application.cancel_instance("t_confirm_recovery", "i_image_1")

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

    def test_delivery_completion_requires_live_kind_and_mime_matches(self) -> None:
        created = create_task(self.commands, "t_delivery_application", "auto")
        draft = image_plan("t_delivery_application")
        saved = self.application.save_plan_and_create_instances(
            "t_delivery_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake"},
            operation_id="prepare_delivery_application",
            envelope=envelope("prepare-delivery-application", created["revision"]),
        )
        starting = self.commands.transition_instance(
            "t_delivery_application",
            "i_image_1",
            "STARTING",
            envelope("delivery-starting", saved["task_revision"], "adapter"),
        )
        running = self.commands.transition_instance(
            "t_delivery_application",
            "i_image_1",
            "RUNNING",
            envelope("delivery-running", starting["task_revision"], "adapter"),
        )
        instance_root = self.assets.initialize_instance_workspace(
            "t_delivery_application", "i_image_1"
        )
        wrong_kind = instance_root / "outputs" / "final.md"
        wrong_kind.write_text("# not an image\n", encoding="utf-8")
        incomplete = self.application.publish_delivery_and_complete(
            "t_delivery_application",
            "i_image_1",
            source_relative_path="instances/i_image_1/outputs/final.md",
            role="final_artwork",
            description="Wrong kind regression",
            operation_id="publish_wrong_kind",
            envelope=envelope("complete-wrong-kind", running["task_revision"], "adapter"),
        )
        self.assertFalse(incomplete["complete"])

        final_image = instance_root / "outputs" / "final.png"
        final_image.write_bytes(b"\x89PNG\r\n\x1a\napplication-delivery")
        completed = self.application.publish_delivery_and_complete(
            "t_delivery_application",
            "i_image_1",
            source_relative_path="instances/i_image_1/outputs/final.png",
            role="final_artwork",
            description="Verified final artwork",
            operation_id="publish_final_image",
            envelope=envelope("complete-final-image", running["task_revision"], "adapter"),
        )
        self.assertTrue(completed["complete"])
        self.assertEqual(completed["transition"]["task"]["status"], "SUCCEEDED")

    def test_waiting_observation_resolves_once_after_crash_and_handles_inbox(self) -> None:
        created = create_task(self.commands, "t_approval_application", "auto")
        draft = image_plan("t_approval_application")
        saved = self.application.save_plan_and_create_instances(
            "t_approval_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake"},
            operation_id="prepare_approval_application",
            envelope=envelope("prepare-approval-application", created["revision"]),
        )
        starting = self.commands.transition_instance(
            "t_approval_application",
            "i_image_1",
            "STARTING",
            envelope("approval-starting", saved["task_revision"], "adapter"),
        )
        self.commands.transition_instance(
            "t_approval_application",
            "i_image_1",
            "RUNNING",
            envelope("approval-running", starting["task_revision"], "adapter"),
        )
        self.fake_adapter.observation = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={
                "job_id": "job_approval",
                "approval_context": {"taskbook": "frozen"},
            },
        )

        observed = self.application.observe_instance(
            "t_approval_application", "i_image_1"
        )
        replayed_observation = self.application.observe_instance(
            "t_approval_application", "i_image_1"
        )
        approval = observed["approval"]["approval"]
        self.assertEqual(
            replayed_observation["approval"]["approval"]["approval_id"],
            approval["approval_id"],
        )
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 1)

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_adapter_advance":
                raise SimulatedCrash(checkpoint)

        resolve_envelope = envelope(
            "resolve-approval-application",
            observed["approval"]["approval_revision"],
        )
        with self.assertRaises(SimulatedCrash):
            self.application.resolve_approval(
                approval["approval_id"],
                decision="APPROVED",
                action="approve_taskbook",
                payload={},
                operation_id="resolve_approval_application",
                envelope=resolve_envelope,
                crash_hook=crash,
            )
        self.assertEqual(len(self.fake_adapter.advance_calls), 1)

        recovered = self.application.recover()
        recovered_result = next(
            item for item in recovered if item["operation_id"] == "resolve_approval_application"
        )
        self.assertEqual(recovered_result["status"], "RECOVERED")
        self.assertEqual(len(self.fake_adapter.advance_calls), 1)
        replay = self.application.resolve_approval(
            approval["approval_id"],
            decision="APPROVED",
            action="approve_taskbook",
            payload={},
            operation_id="resolve_approval_application",
            envelope=resolve_envelope,
        )
        self.assertEqual(replay["approval"]["status"], "APPROVED")
        self.assertEqual(replay["instance"]["status"], "RUNNING")
        self.assertEqual(self.approvals.list_inbox(owner="human")[0]["status"], "HANDLED")

    def test_completed_observation_publishes_required_asset_before_success(self) -> None:
        created = create_task(self.commands, "t_collect_application", "auto")
        draft = image_plan("t_collect_application")
        saved = self.application.save_plan_and_create_instances(
            "t_collect_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake"},
            operation_id="prepare_collect_application",
            envelope=envelope("prepare-collect-application", created["revision"]),
        )
        starting = self.commands.transition_instance(
            "t_collect_application",
            "i_image_1",
            "STARTING",
            envelope("collect-starting", saved["task_revision"], "adapter"),
        )
        self.commands.transition_instance(
            "t_collect_application",
            "i_image_1",
            "RUNNING",
            envelope("collect-running", starting["task_revision"], "adapter"),
        )
        content = b"\x89PNG\r\n\x1a\ncollected-delivery"
        instance_root = self.assets.initialize_instance_workspace(
            "t_collect_application", "i_image_1"
        )
        output = instance_root / "outputs" / "final.png"
        output.write_bytes(content)
        self.fake_adapter.deliveries = [
            {
                "source_relative_path": "instances/i_image_1/outputs/final.png",
                "kind": "image",
                "role": "final_artwork",
                "description": "Collected final artwork",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
        self.fake_adapter.observation = AdapterObservation(
            "RUNNING",
            step_id="completed",
            details={"completed": True},
        )

        completed = self.application.observe_instance(
            "t_collect_application", "i_image_1"
        )
        self.assertEqual(completed["instance"]["status"], "SUCCEEDED")
        published = self.assets.list_assets("t_collect_application")
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0]["manifest"]["producer_instance_id"], "i_image_1")
        self.assertEqual(published[0]["manifest"]["role"], "final_artwork")
        notifications = self.approvals.list_inbox(owner="human")
        self.assertEqual(
            [item["kind"] for item in notifications],
            ["INSTANCE_SUCCEEDED", "TASK_SUCCEEDED"],
        )
        self.application.observe_instance("t_collect_application", "i_image_1")
        self.assertEqual(len(self.assets.list_assets("t_collect_application")), 1)
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 2)
        self.assertEqual(self.application.recover(), [])
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 2)

    def test_concurrent_approval_operations_cannot_advance_the_agent_twice(self) -> None:
        created = create_task(self.commands, "t_concurrent_approval", "auto")
        draft = image_plan("t_concurrent_approval")
        saved = self.application.save_plan_and_create_instances(
            "t_concurrent_approval",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            providers={"i_image_1": "fake"},
            operation_id="prepare_concurrent_approval",
            envelope=envelope("prepare-concurrent-approval", created["revision"]),
        )
        starting = self.commands.transition_instance(
            "t_concurrent_approval",
            "i_image_1",
            "STARTING",
            envelope("concurrent-approval-starting", saved["task_revision"], "adapter"),
        )
        self.commands.transition_instance(
            "t_concurrent_approval",
            "i_image_1",
            "RUNNING",
            envelope("concurrent-approval-running", starting["task_revision"], "adapter"),
        )
        self.fake_adapter.observation = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={"job_id": "job_concurrent_approval"},
        )
        observed = self.application.observe_instance(
            "t_concurrent_approval", "i_image_1"
        )
        approval = observed["approval"]
        self.fake_adapter.advance_delay = 0.1

        def resolve(index: int):
            try:
                return self.application.resolve_approval(
                    approval["approval"]["approval_id"],
                    decision="APPROVED",
                    action="approve_taskbook",
                    payload={},
                    operation_id=f"resolve_concurrent_approval_{index}",
                    envelope=envelope(
                        f"resolve-concurrent-approval-{index}",
                        approval["approval_revision"],
                    ),
                )
            except HarnessError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(resolve, (1, 2)))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, HarnessError) for item in outcomes), 1)
        self.assertEqual(len(self.fake_adapter.advance_calls), 1)


if __name__ == "__main__":
    unittest.main()
