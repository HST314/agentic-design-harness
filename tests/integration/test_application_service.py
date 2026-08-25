from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from harness.adapters import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    AdapterRegistry,
    PptAgentContractAdapter,
    ValidationResult,
)
from harness.adapters.image import ImageAgentAdapter
from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.agent_config_materialization import ImageAgentConfigMaterializer
from harness.services.application import HarnessApplicationService
from harness.services.approvals import ApprovalInboxService
from harness.services.assets import AssetService
from harness.services.instance_runtime_settings import InstanceRuntimeSettingsService
from harness.services.process_runtime import AgentRuntimeArtifact, ProcessSpec
from harness.services.runtime_config_observability import RuntimeConfigObservability
from harness.services.supervisor import ProcessSupervisor
from harness.services.task_config import TaskConfigService
from harness.storage.atomic import read_json
from runtime_helpers import (
    build_config_snapshot,
    build_service,
    create_task,
    envelope,
    image_plan,
    ppt_plan,
)

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agent_process.py"
class FakeImageAdapter:
    agent_type = "image"
    available = True

    def __init__(self) -> None:
        self.runtime_spec = None
        self.start_calls = []
        self.stop_calls = []
        self.advance_calls = []
        self.observation = AdapterObservation("RUNNING")
        self.deliveries = []
        self.delivery_bundles = []
        self.advance_delay = 0.0
        self.advance_accepted = True
        self.validation_delegate = None
        self.prepare_error = None
        self.start_error = None

    def validate_task_card(self, card):
        if self.validation_delegate is not None:
            return self.validation_delegate(card)
        errors = () if card.get("agent_type") == "image" else ("wrong agent type",)
        return ValidationResult(valid=not errors, errors=errors)

    def prepare(self, request):
        if self.prepare_error is not None:
            raise self.prepare_error
        if self.runtime_spec is None:
            raise AssertionError("no runtime artifact was configured for this test")
        return self.runtime_spec

    def start(self, instance_id, operation_id):
        if self.start_error is not None:
            raise self.start_error
        self.start_calls.append((instance_id, operation_id))
        return AdapterCommandResult(True, operation_id)

    def stop(self, instance_id, reason, operation_id):
        self.stop_calls.append((instance_id, reason, operation_id))
        return AdapterCommandResult(True, operation_id, {"reason": reason})

    def get_status(self, instance_id):
        return self.observation

    def request_advance(self, instance_id, action, payload, operation_id):
        if self.advance_delay:
            time.sleep(self.advance_delay)
        self.advance_calls.append((instance_id, action, payload, operation_id))
        return AdapterCommandResult(
            self.advance_accepted,
            operation_id,
            {"reason": "scripted rejection"} if not self.advance_accepted else {},
        )

    def collect_deliveries(self, instance_id):
        return self.deliveries

    def collect_delivery_bundles(self, instance_id):
        return self.delivery_bundles

    def collect_usage(self, instance_id, cursor):
        return []

    def get_ui_url(self, instance_id):
        return None

    def validate_ui_url(self, instance, ui_url):
        return ValidationResult(False, ("fake adapter has no UI",))

    def recover(self, instance_snapshot):
        return AdapterRecoveryResult(True, "RUNNING")


class HarnessApplicationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        self.assets = AssetService(self.store)
        self.approvals = ApprovalInboxService(self.store)
        self.task_config = TaskConfigService(self.store, build_config_snapshot())
        self.image_config = ImageAgentConfigMaterializer(self.store, self.task_config)
        self.supervisor = ProcessSupervisor(self.store, self.commands, self.image_config)
        self.fake_adapter = FakeImageAdapter()
        self.adapters = AdapterRegistry([self.fake_adapter, PptAgentContractAdapter()])
        self.runtime_settings = InstanceRuntimeSettingsService(
            self.store,
            self.task_config,
            self.image_config,
            self.adapters,
            RuntimeConfigObservability(self.store),
        )
        self.application = HarnessApplicationService(
            self.store,
            self.commands,
            self.assets,
            self.approvals,
            self.supervisor,
            self.adapters,
            self.task_config,
            self.runtime_settings,
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
        (artifact_root / "requirements.lock").write_text("stdlib-only\n", encoding="utf-8")
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
            operation_id="save_application_plan",
            envelope=envelope("save-application-plan", created["revision"]),
        )
        replay = self.application.save_plan_and_create_instances(
            "t_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_application_plan",
            envelope=envelope("save-application-plan", created["revision"]),
        )
        self.assertEqual(replay, result)
        self.assertTrue(
            all("credential_pair_ref" not in item for item in result["plan"]["instances"])
        )
        self.assertEqual(result["plan"]["task_cards"][0]["schema_version"], "1.1")

    def test_crash_after_plan_commit_recovers_idempotently(self) -> None:
        created = create_task(self.commands, "t_recover_application")
        draft = image_plan("t_recover_application", 2)

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_plan_commit":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.save_plan_and_create_instances(
                "t_recover_application",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                operation_id="recover_application_plan",
                envelope=envelope("recover-application-plan", created["revision"]),
                crash_hook=crash,
            )
        self.assertIsNotNone(
            self.store.plan.get("t_recover_application", "t_recover_application")
        )
        recovered = self.application.recover()
        self.assertEqual(recovered[0]["status"], "RECOVERED")
        self.assertIsNotNone(self.store.plan.get("t_recover_application", "t_recover_application"))

    def test_invalid_plan_is_rejected_before_an_intent_is_written(self) -> None:
        created = create_task(self.commands, "t_invalid_application")
        draft = image_plan("t_invalid_application", 2)
        draft["stages"][0]["instance_ids"] = ["i_image_1"]
        with self.assertRaises(HarnessError) as invalid:
            self.application.save_plan_and_create_instances(
                "t_invalid_application",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                operation_id="invalid_application_plan",
                envelope=envelope("invalid-application-plan", created["revision"]),
            )
        self.assertEqual(invalid.exception.code, "VALIDATION_ERROR")
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
                operation_id="stale_application_plan",
                envelope=envelope("stale-application-plan", created["revision"]),
            )

        self.assertEqual(stale.exception.code, "REVISION_CONFLICT")
        self.assertIsNone(self.store.instance.get("t_stale_application", "i_image_1"))
        self.assertFalse(self.application._intent_path("stale_application_plan").exists())
        self.assertEqual(self.application.recover(), [])

    def test_revision_advance_after_intent_aborts_without_writing_instances(self) -> None:
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
        self.assertIsNone(self.store.instance.get("t_intent_revision_advance", "i_image_1"))
        self.assertIsNone(self.store.instance.get("t_intent_revision_advance", "i_image_2"))
        self.assertEqual(self.application.recover(), [])

    def test_unavailable_ppt_plan_saves_without_deployment_configuration(self) -> None:
        created = create_task(self.commands, "t_ppt_application")
        draft = ppt_plan("t_ppt_application")
        result = self.application.save_plan_and_create_instances(
            "t_ppt_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_ppt_application_plan",
            envelope=envelope("save-ppt-application-plan", created["revision"]),
        )
        self.assertEqual(result["plan"]["instances"][0]["status"], "UNAVAILABLE")
        self.assertNotIn("credential_pair_ref", result["plan"]["instances"][0])

    def test_start_intent_replays_adapter_start_after_process_crash_window(self) -> None:
        self._configure_runtime_artifact("application-fake-agent")
        created = create_task(self.commands, "t_start_application")
        draft = image_plan("t_start_application")
        saved = self.application.save_plan_and_create_instances(
            "t_start_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
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
                envelope=envelope("start-application-instances", saved["task_revision"]),
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
        self.assertEqual(len(self.fake_adapter.stop_calls), 1)

    def test_start_intent_is_durable_before_manual_confirmation(self) -> None:
        self._configure_runtime_artifact("pre-confirmation-fake-agent")
        created = create_task(self.commands, "t_confirm_recovery")
        draft = image_plan("t_confirm_recovery")
        saved = self.application.save_plan_and_create_instances(
            "t_confirm_recovery",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
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
        locked = self.task_config.get_current("t_confirm_recovery")["state"]
        self.assertEqual(locked["locked_reason"], "launch_intent_accepted")
        self.assertIsNotNone(locked["locked_at"])
        recovered = self.application.recover()
        self.assertEqual(recovered[0]["status"], "RECOVERED")
        self.assertEqual(len(self.fake_adapter.start_calls), 1)
        self.application.cancel_instance("t_confirm_recovery", "i_image_1")

    def test_prompt_only_image_card_can_be_confirmed_and_started(self) -> None:
        self._configure_runtime_artifact("prompt-only-image-fake-agent")
        contract_adapter = ImageAgentAdapter(
            self.store,
            self.store.contracts,
            self.assets,
            self.image_config,
            source_root=self.root,
            interpreter=Path(sys.executable),
            dependency_root=self.root,
        )
        self.fake_adapter.validation_delegate = contract_adapter.validate_task_card
        created = create_task(self.commands, "t_prompt_only_image")
        draft = image_plan("t_prompt_only_image")
        card = draft["task_cards"][0]
        card.update(
            {
                "schema_version": "1.1",
                "created_at": "2026-08-24T08:00:00Z",
            }
        )
        card["parameters"]["usage_context"] = "Internal campaign review."
        saved = self.application.save_plan_and_create_instances(
            "t_prompt_only_image",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save-prompt-only-image",
            envelope=envelope("save-prompt-only-image", created["revision"]),
        )

        started = self.application.confirm_and_start_ready_instances(
            "t_prompt_only_image",
            operation_id="start-prompt-only-image",
            envelope=envelope("start-prompt-only-image", saved["task_revision"]),
        )

        self.assertEqual(len(started["launches"]), 1)
        self.assertEqual(len(self.fake_adapter.start_calls), 1)
        self.application.cancel_instance("t_prompt_only_image", "i_image_1")

    def test_start_failure_is_persisted_and_retry_resumes_the_same_operation(self) -> None:
        created = create_task(self.commands, "t_start_failure")
        draft = image_plan("t_start_failure")
        saved = self.application.save_plan_and_create_instances(
            "t_start_failure",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save-start-failure",
            envelope=envelope("save-start-failure", created["revision"]),
        )
        self.fake_adapter.prepare_error = HarnessError(
            "PROCESS_START_FAILED",
            "The verified runtime artifact was unavailable.",
            {"failure_type": "InjectedPrepareFailure"},
        )

        failed = self.application.confirm_and_start_ready_instances(
            "t_start_failure",
            operation_id="start-failure-operation",
            envelope=envelope("start-failure-operation", saved["task_revision"]),
        )

        self.assertEqual(failed["state"], "RETRYABLE_FAILED")
        instance = self.store.instance.get("t_start_failure", "i_image_1")
        self.assertEqual(instance["status"], "FAILED_TO_START")
        self.assertEqual(instance["start_failure"]["phase"], "PREPARING")
        self.assertNotIn("request", failed)

        self.fake_adapter.prepare_error = None
        self._configure_runtime_artifact("retry-start-failure-agent")
        retry_envelope = envelope(
            "retry-start-failure",
            self.store.task.revision("t_start_failure", "t_start_failure"),
        )
        retried = self.application.retry_start_operation(
            "start-failure-operation",
            envelope=retry_envelope,
        )

        self.assertEqual(retried["state"], "COMMITTED")
        instance = self.store.instance.get("t_start_failure", "i_image_1")
        self.assertEqual(instance["status"], "RUNNING")
        self.assertIsNone(instance.get("start_failure"))
        self.assertEqual(len(self.fake_adapter.start_calls), 1)
        self.assertEqual(
            self.application.retry_start_operation(
                "start-failure-operation", envelope=retry_envelope
            )["state"],
            "COMMITTED",
        )
        with self.assertRaises(HarnessError) as conflict:
            self.application.retry_start_operation(
                "start-failure-operation",
                envelope=envelope(
                    "retry-start-failure",
                    self.store.task.revision("t_start_failure", "t_start_failure"),
                ),
            )
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        self.application.cancel_instance("t_start_failure", "i_image_1")

    def test_adapter_rejection_after_process_ready_is_failed_to_start(self) -> None:
        created = create_task(self.commands, "t_agent_start_failure")
        draft = image_plan("t_agent_start_failure")
        saved = self.application.save_plan_and_create_instances(
            "t_agent_start_failure",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save-agent-start-failure",
            envelope=envelope("save-agent-start-failure", created["revision"]),
        )
        self._configure_runtime_artifact("agent-start-failure-agent")
        self.fake_adapter.start_error = HarnessError(
            "PROCESS_START_FAILED",
            "The Agent rejected its managed start request.",
            {"http_status": 503, "route": "api/projects/i_image_1/jobs"},
        )

        failed = self.application.confirm_and_start_ready_instances(
            "t_agent_start_failure",
            operation_id="agent-start-failure-operation",
            envelope=envelope("agent-start-failure-operation", saved["task_revision"]),
        )

        self.assertEqual(failed["state"], "RETRYABLE_FAILED")
        instance_state = self.store.instance.get(
            "t_agent_start_failure", "i_image_1"
        )
        self.assertEqual(instance_state["status"], "FAILED_TO_START")
        self.assertEqual(instance_state["process"]["state"], "RUNNING")
        self.assertEqual(instance_state["start_failure"]["phase"], "AGENT_STARTING")
        self.assertEqual(
            instance_state["start_failure"]["details"],
            {"http_status": 503, "route": "api/projects/i_image_1/jobs"},
        )
        self.application.cancel_instance("t_agent_start_failure", "i_image_1")

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
                    operation_id=f"concurrent_application_{index}",
                    envelope=envelope(f"concurrent-application-{index}", created["revision"]),
                )
            except HarnessError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(save, (1, 2)))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        self.assertEqual(sum(isinstance(item, HarnessError) for item in outcomes), 1)
        plan = self.store.plan.get("t_concurrent_application", "t_concurrent_application")
        self.assertEqual(len(plan["instances"]), 2)

    def test_task_cancel_replays_every_child_and_commit_crash_window(self) -> None:
        checkpoints = [
            *(f"before_instance_cancel:i_image_{index}" for index in range(1, 4)),
            *(f"after_instance_cancel_effect:i_image_{index}" for index in range(1, 4)),
            "before_task_cancel_commit",
            "after_task_cancel_commit",
        ]
        for index, checkpoint in enumerate(checkpoints, 1):
            with self.subTest(checkpoint=checkpoint):
                task_id = f"t_cancel_crash_{index}"
                revision = self._running_image_task(task_id, 3)
                command = envelope(f"cancel-crash-{index}", revision)

                def crash(current: str, target: str = checkpoint) -> None:
                    if current == target:
                        raise SimulatedCrash(current)

                with self.assertRaises(SimulatedCrash):
                    self.application.cancel_task(
                        task_id,
                        operation_id=f"cancel_crash_{index}",
                        envelope=command,
                        crash_hook=crash,
                    )

                # The exact original request must resume even though completed
                # child cancellations have advanced the aggregate revision.
                result = self.application.cancel_task(
                    task_id,
                    operation_id=f"cancel_crash_{index}",
                    envelope=command,
                )
                replay = self.application.cancel_task(
                    task_id,
                    operation_id=f"cancel_crash_{index}",
                    envelope=command,
                )
                self.assertEqual(replay, result)
                self.assertEqual(result["task"]["status"], "CANCELLED")
                self.assertEqual(
                    [item["status"] for item in result["plan"]["instances"]],
                    ["CANCELLED", "CANCELLED", "CANCELLED"],
                )
                intent = read_json(self.application._intent_path(f"cancel_crash_{index}"))
                self.assertEqual(intent["state"], "COMMITTED")
                self.assertEqual(
                    [item["initial_status"] for item in intent["target_instances"]],
                    ["RUNNING", "RUNNING", "RUNNING"],
                )
                self.assertEqual(
                    {item["state"] for item in intent["instance_progress"].values()},
                    {"CANCELLED"},
                )

    def test_startup_recovery_resumes_a_partial_task_cancel(self) -> None:
        task_id = "t_cancel_startup_recovery"
        revision = self._running_image_task(task_id, 3)
        command = envelope("cancel-startup-recovery", revision)

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_instance_cancel_effect:i_image_2":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.cancel_task(
                task_id,
                operation_id="cancel_startup_recovery",
                envelope=command,
                crash_hook=crash,
            )
        partial = self.store.plan.get(task_id, task_id)
        self.assertEqual(
            [item["status"] for item in partial["instances"]],
            ["CANCELLED", "CANCELLED", "RUNNING"],
        )

        recovered = self.application.recover()

        recovery = next(
            item for item in recovered if item["operation_id"] == "cancel_startup_recovery"
        )
        self.assertEqual(recovery["status"], "RECOVERED")
        self.assertEqual(recovery["result"]["task"]["status"], "CANCELLED")
        self.assertEqual(
            [item["status"] for item in recovery["result"]["plan"]["instances"]],
            ["CANCELLED", "CANCELLED", "CANCELLED"],
        )
        replay = self.application.cancel_task(
            task_id,
            operation_id="cancel_startup_recovery",
            envelope=command,
        )
        self.assertEqual(replay, recovery["result"])

    def test_delivery_completion_requires_live_kind_and_mime_matches(self) -> None:
        created = create_task(self.commands, "t_delivery_application", "auto")
        draft = image_plan("t_delivery_application")
        saved = self.application.save_plan_and_create_instances(
            "t_delivery_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
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
            envelope=envelope(
                "complete-wrong-kind",
                running["task_revision"],
                "adapter",
                "image_adapter",
            ),
        )
        self.assertFalse(incomplete["complete"])
        self.assertEqual(self.assets.list_assets("t_delivery_application"), [])

        final_image = instance_root / "outputs" / "final.png"
        final_image.write_bytes(b"\x89PNG\r\n\x1a\napplication-delivery")
        completed = self.application.publish_delivery_and_complete(
            "t_delivery_application",
            "i_image_1",
            source_relative_path="instances/i_image_1/outputs/final.png",
            role="final_artwork",
            description="Verified final artwork",
            operation_id="publish_final_image",
            envelope=envelope(
                "complete-final-image",
                running["task_revision"],
                "adapter",
                "image_adapter",
            ),
        )
        self.assertTrue(completed["complete"])
        self.assertEqual(completed["transition"]["task"]["status"], "SUCCEEDED")
        self.assertEqual(
            [item["kind"] for item in self.approvals.list_inbox(owner="human")],
            ["INSTANCE_SUCCEEDED", "TASK_SUCCEEDED"],
        )

        replay = self.application.publish_delivery_and_complete(
            "t_delivery_application",
            "i_image_1",
            source_relative_path="instances/i_image_1/outputs/final.png",
            role="final_artwork",
            description="Verified final artwork",
            operation_id="publish_final_image",
            envelope=envelope(
                "complete-final-image",
                running["task_revision"],
                "adapter",
                "image_adapter",
            ),
        )
        self.assertEqual(replay, completed)
        self.assertEqual(len(self.assets.list_assets("t_delivery_application")), 1)
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 2)

        with self.assertRaises(HarnessError) as new_delivery:
            self.application.publish_delivery_and_complete(
                "t_delivery_application",
                "i_image_1",
                source_relative_path="instances/i_image_1/outputs/final.png",
                role="final_artwork",
                description="Unexpected second publication",
                operation_id="publish_after_success",
                envelope=envelope(
                    "complete-final-image",
                    running["task_revision"],
                    "adapter",
                    "image_adapter",
                ),
            )
        self.assertEqual(new_delivery.exception.code, "INVALID_STATE_TRANSITION")
        self.assertEqual(len(self.assets.list_assets("t_delivery_application")), 1)

    def test_delivery_command_rejects_wrong_actor_before_publication(self) -> None:
        created = create_task(self.commands, "t_delivery_actor", "auto")
        draft = image_plan("t_delivery_actor")
        saved = self.application.save_plan_and_create_instances(
            "t_delivery_actor",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="prepare_delivery_actor",
            envelope=envelope("prepare-delivery-actor", created["revision"]),
        )
        starting = self.commands.transition_instance(
            "t_delivery_actor",
            "i_image_1",
            "STARTING",
            envelope("delivery-actor-starting", saved["task_revision"], "adapter"),
        )
        running = self.commands.transition_instance(
            "t_delivery_actor",
            "i_image_1",
            "RUNNING",
            envelope("delivery-actor-running", starting["task_revision"], "adapter"),
        )
        instance_root = self.assets.initialize_instance_workspace("t_delivery_actor", "i_image_1")
        (instance_root / "outputs" / "final.png").write_bytes(b"\x89PNG\r\n\x1a\nactor-gate")

        with self.assertRaises(HarnessError) as denied:
            self.application.publish_delivery_and_complete(
                "t_delivery_actor",
                "i_image_1",
                source_relative_path="instances/i_image_1/outputs/final.png",
                role="final_artwork",
                description="Actor gate regression",
                operation_id="publish_wrong_actor",
                envelope=envelope("complete-wrong-actor", running["task_revision"], "human"),
            )

        self.assertEqual(denied.exception.code, "VALIDATION_ERROR")
        self.assertEqual(self.assets.list_assets("t_delivery_actor"), [])

    def test_waiting_observation_resolves_once_after_crash_and_handles_inbox(self) -> None:
        created = create_task(self.commands, "t_approval_application", "auto")
        draft = image_plan("t_approval_application")
        saved = self.application.save_plan_and_create_instances(
            "t_approval_application",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
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

        observed = self.application.observe_instance("t_approval_application", "i_image_1")
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

    def test_rejected_adapter_advance_keeps_approval_and_instance_pending(self) -> None:
        observed = self._waiting_approval("t_rejected_advance")
        approval = observed["approval"]
        self.fake_adapter.advance_accepted = False
        resolve_envelope = envelope(
            "resolve-rejected-advance",
            approval["approval_revision"],
        )

        for _ in range(2):
            with self.assertRaises(HarnessError) as rejected:
                self.application.resolve_approval(
                    approval["approval"]["approval_id"],
                    decision="APPROVED",
                    action="approve_taskbook",
                    payload={},
                    operation_id="resolve_rejected_advance",
                    envelope=resolve_envelope,
                )
            self.assertEqual(rejected.exception.code, "INVALID_STATE_TRANSITION")
            self.assertIn("no decision was committed", rejected.exception.message)

        self.assertEqual(len(self.fake_adapter.advance_calls), 1)
        details = self.approvals.get_approval(approval["approval"]["approval_id"])
        self.assertEqual(details["approval"]["status"], "PENDING")
        self.assertEqual(
            self.store.instance.get("t_rejected_advance", "i_image_1")["status"],
            "WAITING_APPROVAL",
        )
        inbox = self.approvals.list_inbox(owner="human")
        self.assertEqual(
            [(item["kind"], item["status"]) for item in inbox], [("APPROVAL_REQUIRED", "UNREAD")]
        )
        self.assertEqual(
            read_json(self.application._intent_path("resolve_rejected_advance"))["state"],
            "ABORTED",
        )

    def test_pending_approval_blocks_delivery_before_asset_publication(self) -> None:
        observed = self._waiting_approval("t_pending_delivery")
        instance_root = self.assets.initialize_instance_workspace("t_pending_delivery", "i_image_1")
        (instance_root / "outputs" / "final.png").write_bytes(b"\x89PNG\r\n\x1a\npending-gate")

        with self.assertRaises(HarnessError) as blocked:
            self.application.publish_delivery_and_complete(
                "t_pending_delivery",
                "i_image_1",
                source_relative_path="instances/i_image_1/outputs/final.png",
                role="final_artwork",
                description="Pending approval bypass regression",
                operation_id="publish_pending_delivery",
                envelope=envelope(
                    "complete-pending-delivery",
                    observed["transition"]["task_revision"],
                    "adapter",
                    "image_adapter",
                ),
            )

        self.assertEqual(blocked.exception.code, "INVALID_STATE_TRANSITION")
        self.assertIn("pending approval", blocked.exception.message.lower())
        self.assertEqual(self.assets.list_assets("t_pending_delivery"), [])
        self.assertEqual(
            self.store.instance.get("t_pending_delivery", "i_image_1")["status"],
            "WAITING_APPROVAL",
        )
        approval = self.approvals.get_approval(observed["approval"]["approval"]["approval_id"])
        self.assertEqual(approval["approval"]["status"], "PENDING")
        self.assertEqual(
            [item["kind"] for item in self.approvals.list_inbox(owner="human")],
            ["APPROVAL_REQUIRED"],
        )

    def test_bundle_delivery_waits_for_human_and_publishes_two_assets_atomically(self) -> None:
        task_id = "t_bundle_delivery"
        self._running_image_task(task_id, 1)
        image = b"\x89PNG\r\n\x1a\nbundle-final-image"
        note = b"# Branch design note\n\nImmutable delivery rationale.\n"
        alternate_image = b"\x89PNG\r\n\x1a\nalternate-branch-image"
        alternate_note = b"# Alternate branch note\n"
        instance_root = self.assets.initialize_instance_workspace(task_id, "i_image_1")
        (instance_root / "outputs" / "bundle-image.png").write_bytes(image)
        (instance_root / "outputs" / "bundle-note.md").write_bytes(note)
        (instance_root / "outputs" / "alternate-image.png").write_bytes(alternate_image)
        (instance_root / "outputs" / "alternate-note.md").write_bytes(alternate_note)
        card = self.store.plan.get(task_id, task_id)["task_cards"][0]
        candidate = {
            "schema_version": "1.0",
            "bundle_id": "bundle_branch_main_01",
            "task_id": task_id,
            "work_item_id": "work_bundle_main_01",
            "instance_id": "i_image_1",
            "task_card_revision": card["revision"],
            "branch_id": "main",
            "checkpoint_id": "checkpoint_0123456789abcdef01234567",
            "image": {
                "private_relative_path": "instances/i_image_1/outputs/bundle-image.png",
                "mime_type": "image/png",
                "size_bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "width": 1920,
                "height": 1080,
            },
            "design_note": {
                "private_relative_path": "instances/i_image_1/outputs/bundle-note.md",
                "mime_type": "text/markdown",
                "size_bytes": len(note),
                "sha256": hashlib.sha256(note).hexdigest(),
            },
            "status": "PENDING_CONFIRMATION",
            "created_at": "2026-08-22T17:00:00Z",
            "decided_at": None,
            "actor": None,
            "publication_batch_id": None,
        }
        alternate = deepcopy(candidate)
        alternate.update(
            bundle_id="bundle_branch_alternate_02",
            branch_id="branch_alternate",
            checkpoint_id="checkpoint_89abcdef0123456701234567",
            created_at="2026-08-22T17:01:00Z",
        )
        alternate["image"].update(
            private_relative_path="instances/i_image_1/outputs/alternate-image.png",
            size_bytes=len(alternate_image),
            sha256=hashlib.sha256(alternate_image).hexdigest(),
        )
        alternate["design_note"].update(
            private_relative_path="instances/i_image_1/outputs/alternate-note.md",
            size_bytes=len(alternate_note),
            sha256=hashlib.sha256(alternate_note).hexdigest(),
        )
        self.fake_adapter.delivery_bundles = [candidate, alternate]
        self.fake_adapter.observation = AdapterObservation(
            "RUNNING", step_id="completed", details={"completed": True}
        )

        observed = self.application.observe_instance(task_id, "i_image_1")
        self.assertEqual(observed["instance"]["status"], "WAITING_APPROVAL")
        self.assertEqual(self.assets.list_assets(task_id), [])
        self.assertEqual(self.assets.list_bundle_manifests(task_id), [])
        approval = observed["delivery"]["approval"]
        alternate_approval = observed["delivery"]["approvals"][1]
        self.assertEqual(approval["approval"]["kind"], "DELIVERY_REVIEW")

        resolved = self.application.resolve_approval(
            approval["approval"]["approval_id"],
            decision="APPROVED",
            action="publish_bundle",
            payload={},
            operation_id="publish_bundle_main_01",
            envelope=envelope(
                "publish-bundle-main-01", approval["approval_revision"]
            ),
        )

        self.assertEqual(resolved["instance"]["status"], "SUCCEEDED")
        self.assertEqual(resolved["candidate"]["status"], "PUBLISHED")
        assets = self.assets.list_assets(task_id)
        self.assertEqual(
            {(item["manifest"]["role"], item["manifest"]["mime_type"]) for item in assets},
            {("final_artwork", "image/png"), ("design_note", "text/markdown")},
        )
        bundles = self.assets.list_bundle_manifests(task_id)
        self.assertEqual(len(bundles), 1)
        self.assertEqual(bundles[0]["bundle_id"], candidate["bundle_id"])
        alternate_resolved = self.application.resolve_approval(
            alternate_approval["approval"]["approval_id"],
            decision="APPROVED",
            action="publish_bundle",
            payload={},
            operation_id="publish_bundle_alternate_02",
            envelope=envelope(
                "publish-bundle-alternate-02",
                alternate_approval["approval_revision"],
            ),
        )
        self.assertEqual(alternate_resolved["instance"]["status"], "SUCCEEDED")
        self.assertEqual(len(self.assets.list_assets(task_id)), 4)
        self.assertEqual(
            {item["bundle_id"] for item in self.assets.list_bundle_manifests(task_id)},
            {candidate["bundle_id"], alternate["bundle_id"]},
        )
        replay = self.application.resolve_approval(
            approval["approval"]["approval_id"],
            decision="APPROVED",
            action="publish_bundle",
            payload={},
            operation_id="publish_bundle_main_01",
            envelope=envelope(
                "publish-bundle-main-01", approval["approval_revision"]
            ),
        )
        self.assertEqual(replay["bundle_manifest"], resolved["bundle_manifest"])
        self.assertEqual(len(self.assets.list_assets(task_id)), 4)

    def test_bundle_publication_recovers_after_manifest_write_without_half_visibility(
        self,
    ) -> None:
        task_id = "t_bundle_crash_recovery"
        self._running_image_task(task_id, 1)
        image = b"\x89PNG\r\n\x1a\ncrash-safe-image"
        note = b"# Crash-safe note\n"
        instance_root = self.assets.initialize_instance_workspace(task_id, "i_image_1")
        (instance_root / "outputs" / "crash-image.png").write_bytes(image)
        (instance_root / "outputs" / "crash-note.md").write_bytes(note)
        candidate = {
            "schema_version": "1.0",
            "bundle_id": "bundle_crash_safe_01",
            "task_id": task_id,
            "work_item_id": "work_crash_safe_01",
            "instance_id": "i_image_1",
            "task_card_revision": 1,
            "branch_id": "branch_crash",
            "checkpoint_id": "checkpoint_fedcba987654321001234567",
            "image": {
                "private_relative_path": "instances/i_image_1/outputs/crash-image.png",
                "mime_type": "image/png",
                "size_bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "width": 1200,
                "height": 800,
            },
            "design_note": {
                "private_relative_path": "instances/i_image_1/outputs/crash-note.md",
                "mime_type": "text/markdown",
                "size_bytes": len(note),
                "sha256": hashlib.sha256(note).hexdigest(),
            },
            "status": "PENDING_CONFIRMATION",
            "created_at": "2026-08-22T17:10:00Z",
            "decided_at": None,
            "actor": None,
            "publication_batch_id": None,
        }
        self.fake_adapter.delivery_bundles = [candidate]
        self.fake_adapter.observation = AdapterObservation(
            "RUNNING", step_id="completed", details={"completed": True}
        )
        approval = self.application.observe_instance(task_id, "i_image_1")["delivery"][
            "approval"
        ]

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_bundle_manifest_write":
                raise SimulatedCrash(checkpoint)

        resolve_envelope = envelope(
            "publish-crash-safe-bundle", approval["approval_revision"]
        )
        with self.assertRaises(SimulatedCrash):
            self.application.resolve_approval(
                approval["approval"]["approval_id"],
                decision="APPROVED",
                action="publish_bundle",
                payload={},
                operation_id="publish_crash_safe_bundle",
                envelope=resolve_envelope,
                crash_hook=crash,
            )

        self.assertEqual(self.assets.list_assets(task_id), [])
        self.assertEqual(self.assets.list_bundle_manifests(task_id), [])
        self.assertEqual(
            self.application.list_delivery_bundle_candidates(task_id)[0]["status"],
            "PENDING_CONFIRMATION",
        )

        recovered = self.application.recover()
        result = next(
            item for item in recovered if item["operation_id"] == "publish_crash_safe_bundle"
        )
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(len(self.assets.list_assets(task_id)), 2)
        self.assertEqual(len(self.assets.list_bundle_manifests(task_id)), 1)
        self.assertEqual(
            self.application.list_delivery_bundle_candidates(task_id)[0]["status"],
            "PUBLISHED",
        )
        self.assertEqual(self.application.recover(), [])
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 3)

    def test_delivery_contract_consumes_each_candidate_once_and_rejects_extras(self) -> None:
        task_id = "t_delivery_contract_cardinality"
        created = create_task(self.commands, task_id, "auto")
        draft = image_plan(task_id)
        draft["task_cards"][0]["expected_deliveries"].append(
            {
                "kind": "image",
                "role": "final_artwork",
                "required": True,
                "accepted_mime_types": ["image/png"],
            }
        )
        self.application.save_plan_and_create_instances(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="prepare_delivery_contract_cardinality",
            envelope=envelope("prepare-delivery-contract-cardinality", created["revision"]),
        )
        candidate = {
            "kind": "image",
            "role": "final_artwork",
            "mime_type": "image/png",
            "sha256": "1" * 64,
        }

        self.assertFalse(
            self.application._required_deliveries_satisfied(
                task_id,
                "i_image_1",
                candidate_manifests=[candidate],
            )
        )
        with self.assertRaises(HarnessError) as unexpected:
            self.application._validate_required_delivery_set(
                task_id,
                "i_image_1",
                candidates=[
                    candidate,
                    {**candidate, "role": "undeclared_artwork", "sha256": "2" * 64},
                ],
            )
        self.assertEqual(unexpected.exception.code, "VALIDATION_ERROR")


    def test_publication_batch_is_invisible_until_instance_success(self) -> None:
        task_id = "t_atomic_publication_visibility"
        revision = self._running_image_task(task_id, 1)
        instance_root = self.assets.initialize_instance_workspace(task_id, "i_image_1")
        output = instance_root / "outputs" / "final.png"
        output.write_bytes(b"\x89PNG\r\n\x1a\natomic-visibility")
        manifest = self.assets.publish_delivery(
            task_id,
            "i_image_1",
            source_relative_path="instances/i_image_1/outputs/final.png",
            role="final_artwork",
            description="Atomic visibility gate",
            idempotency_key="atomic-visibility-publication",
            batch_id="batch_atomic_visibility",
        )
        with self.assertRaises(HarnessError) as wrong_owner:
            self.assets.commit_publication_batch(
                task_id,
                "i_image_1",
                batch_id="batch_wrong_owner",
                manifests=[{**manifest, "producer_instance_id": "i_other"}],
            )
        self.assertEqual(wrong_owner.exception.code, "ASSET_VALIDATION_FAILED")
        self.assets.commit_publication_batch(
            task_id,
            "i_image_1",
            batch_id="batch_atomic_visibility",
            manifests=[manifest],
        )

        self.assertEqual(self.assets.list_assets(task_id), [])

        self.commands.transition_instance(
            task_id,
            "i_image_1",
            "SUCCEEDED",
            envelope("atomic-visibility-success", revision, "adapter"),
        )
        self.assertEqual(len(self.assets.list_assets(task_id)), 1)

    def test_concurrent_approval_operations_cannot_advance_the_agent_twice(self) -> None:
        created = create_task(self.commands, "t_concurrent_approval", "auto")
        draft = image_plan("t_concurrent_approval")
        saved = self.application.save_plan_and_create_instances(
            "t_concurrent_approval",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
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
        observed = self.application.observe_instance("t_concurrent_approval", "i_image_1")
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

    def _waiting_approval(self, task_id: str) -> dict:
        created = create_task(self.commands, task_id, "auto")
        draft = image_plan(task_id)
        saved = self.application.save_plan_and_create_instances(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id=f"prepare_{task_id}",
            envelope=envelope(f"prepare-{task_id}", created["revision"]),
        )
        starting = self.commands.transition_instance(
            task_id,
            "i_image_1",
            "STARTING",
            envelope(f"{task_id}-starting", saved["task_revision"], "adapter"),
        )
        self.commands.transition_instance(
            task_id,
            "i_image_1",
            "RUNNING",
            envelope(f"{task_id}-running", starting["task_revision"], "adapter"),
        )
        self.fake_adapter.observation = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={"job_id": f"job_{task_id}"},
        )
        return self.application.observe_instance(task_id, "i_image_1")

    def _running_image_task(self, task_id: str, count: int) -> int:
        created = create_task(self.commands, task_id, "auto")
        draft = image_plan(task_id, count)
        saved = self.commands.save_plan(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope(f"save-{task_id}", created["revision"]),
        )
        revision = saved["task_revision"]
        for instance in draft["instances"]:
            instance_id = instance["instance_id"]
            starting = self.commands.transition_instance(
                task_id,
                instance_id,
                "STARTING",
                envelope(f"start-{task_id}-{instance_id}", revision, "adapter"),
            )
            running = self.commands.transition_instance(
                task_id,
                instance_id,
                "RUNNING",
                envelope(
                    f"run-{task_id}-{instance_id}",
                    starting["task_revision"],
                    "adapter",
                ),
            )
            revision = running["task_revision"]
        return revision


if __name__ == "__main__":
    unittest.main()
