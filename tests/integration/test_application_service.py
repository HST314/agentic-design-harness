from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from harness.adapters import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    AdapterRegistry,
    AgentWorkState,
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
from harness.storage.atomic import atomic_write_json, read_json
from harness.storage.locks import FileLock
from runtime_helpers import (
    build_config_snapshot,
    build_service,
    create_task,
    envelope,
    image_plan,
    image_to_ppt_plan,
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
        self.observation_delay = 0.0
        self.advance_accepted = True
        self.validation_delegate = None
        self.prepare_error = None
        self.start_error = None
        self.prepare_calls = []
        self.work_state = AgentWorkState.IDLE
        self.quiesced = False
        self.quiesce_calls = []
        self.unquiesce_calls = []

    def validate_task_card(self, card):
        if self.validation_delegate is not None:
            return self.validation_delegate(card)
        errors = () if card.get("agent_type") == "image" else ("wrong agent type",)
        return ValidationResult(valid=not errors, errors=errors)

    def prepare(self, request):
        self.prepare_calls.append(request.instance["instance_id"])
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
        if self.observation_delay:
            time.sleep(self.observation_delay)
        return self.observation

    def probe_work_state(self, instance_id):
        return self.work_state

    def quiesce(self, instance_id, operation_id):
        self.quiesced = True
        self.quiesce_calls.append((instance_id, operation_id))
        return AdapterCommandResult(True, operation_id, {"quiesced": True})

    def unquiesce(self, instance_id, operation_id):
        self.quiesced = False
        self.unquiesce_calls.append((instance_id, operation_id))
        return AdapterCommandResult(True, operation_id, {"quiesced": False})

    def submit_concurrent_work(self):
        if self.quiesced:
            raise HarnessError("AGENT_QUIESCED", "The Agent is not accepting new work.")
        self.work_state = AgentWorkState.ACTIVE

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


class FakePptAdapter(FakeImageAdapter):
    agent_type = "ppt"

    def validate_task_card(self, card):
        errors = () if card.get("agent_type") == "ppt" else ("wrong agent type",)
        return ValidationResult(valid=not errors, errors=errors)


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
        self.fake_ppt_adapter = FakePptAdapter()
        self.adapters = AdapterRegistry([self.fake_adapter, self.fake_ppt_adapter])
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
        self.application.close_monitoring()
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
        runtime_policy = artifact_root / "ppt-runtime.yaml"
        model_config = artifact_root / "ppt-model-config.yaml"
        runtime_policy.write_text("schema_version: '1.0'\n", encoding="utf-8")
        model_config.write_text("model_config_id: test\n", encoding="utf-8")
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
            public_environment={
                "PPT_AGENT_RUNTIME_POLICY": str(runtime_policy),
                "PPT_AGENT_MODEL_CONFIG": str(model_config),
            },
        )
        self.fake_ppt_adapter.runtime_spec = self.fake_adapter.runtime_spec

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
        self.assertIsNotNone(self.store.plan.get("t_recover_application", "t_recover_application"))
        recovered = self.application.recover()
        self.assertEqual(recovered[0]["status"], "RECOVERED")
        self.assertIsNotNone(self.store.plan.get("t_recover_application", "t_recover_application"))

    def test_startup_defers_prepared_instance_start_to_background_runner(self) -> None:
        intent_path = self.application._intent_path("recover_deferred_instance_start")
        atomic_write_json(
            intent_path,
            {
                "schema_version": "1.0",
                "kind": "START_INSTANCE",
                "operation_id": "recover_deferred_instance_start",
                "request_sha256": "0" * 64,
                "request": {
                    "task_id": "t_deferred_instance_start",
                    "instance_id": "i_image_1",
                    "envelope": {},
                },
                "state": "PREPARED",
                "prepared_at": "2026-08-28T03:00:00Z",
                "result": None,
            },
        )

        with patch.object(self.application, "_resume_instance_operation") as resume:
            recovered = self.application.recover(defer_start_operations=True)

            resume.assert_not_called()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(
                recovered[0]["operation_id"], "recover_deferred_instance_start"
            )
            self.assertEqual(recovered[0]["status"], "PENDING")
            self.assertEqual(recovered[0]["result"]["state"], "QUEUED")
            self.assertEqual(
                recovered[0]["result"]["instance_progress"]["i_image_1"]["state"],
                "PENDING",
            )

            self.application._run_pending_starts()

        resume.assert_called_once_with(intent_path)

    def test_single_instance_start_releases_task_lock_before_slow_prepare(self) -> None:
        created = create_task(self.commands, "t_async_instance_start")
        draft = image_plan("t_async_instance_start")
        saved = self.application.save_plan_and_create_instances(
            "t_async_instance_start",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_async_instance_start",
            envelope=envelope("save-async-instance-start", created["revision"]),
        )
        confirmed = self.commands.confirm_start(
            "t_async_instance_start",
            envelope("confirm-async-instance-start", saved["task_revision"]),
        )
        self._configure_runtime_artifact("async-instance-start-agent")
        prepare_started = threading.Event()
        release_prepare = threading.Event()
        original_prepare = self.fake_adapter.prepare

        def slow_prepare(request):
            prepare_started.set()
            if not release_prepare.wait(3):
                raise AssertionError("test did not release the slow prepare")
            return original_prepare(request)

        self.application.start_monitoring()
        with patch.object(self.fake_adapter, "prepare", side_effect=slow_prepare):
            queued = self.application.start_instance(
                "t_async_instance_start",
                "i_image_1",
                operation_id="start_async_instance",
                envelope=envelope(
                    "start-async-instance", confirmed["task_revision"]
                ),
            )
            self.assertEqual(queued["state"], "QUEUED")
            self.assertIsNone(
                self.application.latest_start_operation("t_async_instance_start")
            )
            self.assertEqual(
                self.application.latest_start_operation(
                    "t_async_instance_start", instance_id="i_image_1"
                )["operation_id"],
                "start_async_instance",
            )
            self.assertTrue(prepare_started.wait(1))
            with FileLock(self.application._task_lock("t_async_instance_start"), 0.2):
                pass
            release_prepare.set()
            completed = self._wait_for_start_operation("start_async_instance")

        self.assertEqual(completed["state"], "COMMITTED")
        self.application.cancel_instance("t_async_instance_start", "i_image_1")

    def test_failed_single_instance_start_is_not_replayed_until_explicit_retry(self) -> None:
        created = create_task(self.commands, "t_single_start_failure")
        draft = image_plan("t_single_start_failure")
        saved = self.application.save_plan_and_create_instances(
            "t_single_start_failure",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_single_start_failure",
            envelope=envelope("save-single-start-failure", created["revision"]),
        )
        confirmed = self.commands.confirm_start(
            "t_single_start_failure",
            envelope("confirm-single-start-failure", saved["task_revision"]),
        )
        self._configure_runtime_artifact("single-start-failure-agent")
        self.fake_adapter.prepare_error = HarnessError(
            "PROCESS_START_FAILED",
            "The verified runtime artifact was unavailable.",
            {"failure_type": "InjectedPrepareFailure"},
        )
        self.application.start_monitoring()

        queued = self.application.start_instance(
            "t_single_start_failure",
            "i_image_1",
            operation_id="single-start-failure-operation",
            envelope=envelope(
                "single-start-failure-operation", confirmed["task_revision"]
            ),
        )
        self.assertEqual(queued["state"], "QUEUED")
        failed = self._wait_for_start_operation("single-start-failure-operation")
        self.assertEqual(failed["state"], "RETRYABLE_FAILED")
        self.assertEqual(failed["last_error"]["phase"], "PREPARING")
        self.assertEqual(self.fake_adapter.prepare_calls, ["i_image_1"])

        time.sleep(0.6)
        self.assertEqual(self.fake_adapter.prepare_calls, ["i_image_1"])

        self.fake_adapter.prepare_error = None
        retried = self.application.retry_start_operation(
            "single-start-failure-operation",
            envelope=envelope(
                "retry-single-start-failure",
                self.store.task.revision(
                    "t_single_start_failure", "t_single_start_failure"
                ),
            ),
        )
        self.assertEqual(retried["state"], "QUEUED")
        completed = self._wait_for_start_operation("single-start-failure-operation")
        self.assertEqual(completed["state"], "COMMITTED")
        self.assertEqual(self.fake_adapter.prepare_calls, ["i_image_1", "i_image_1"])
        self.application.cancel_instance("t_single_start_failure", "i_image_1")

    def test_sibling_start_queues_while_confirmed_start_is_preparing(self) -> None:
        task_id = "t_sibling_start_during_prepare"
        created = create_task(self.commands, task_id)
        draft = image_plan(task_id, count=2)
        saved = self.application.save_plan_and_create_instances(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_sibling_start_during_prepare",
            envelope=envelope("save-sibling-start-during-prepare", created["revision"]),
        )
        self._configure_runtime_artifact("sibling-start-during-prepare-agent")
        prepare_started = threading.Event()
        release_prepare = threading.Event()
        original_prepare = self.fake_adapter.prepare

        def slow_first_prepare(request):
            if request.instance["instance_id"] == "i_image_1":
                prepare_started.set()
                if not release_prepare.wait(3):
                    raise AssertionError("test did not release the slow prepare")
            return original_prepare(request)

        self.application.start_monitoring()
        with patch.object(self.fake_adapter, "prepare", side_effect=slow_first_prepare):
            first = self.application.confirm_and_start_ready_instances(
                task_id,
                operation_id="confirm_first_sibling",
                envelope=envelope("confirm-first-sibling", saved["task_revision"]),
                only_instance_ids=["i_image_1"],
            )
            self.assertEqual(first["state"], "QUEUED")
            self.assertTrue(prepare_started.wait(1))

            # This is deliberately the page's pre-confirmation task revision. The
            # first start advanced the task aggregate, but not this sibling's card.
            second = self.application.start_instance(
                task_id,
                "i_image_2",
                operation_id="start_second_sibling",
                envelope=envelope("start-second-sibling", saved["task_revision"]),
            )
            self.assertEqual(second["state"], "QUEUED")
            release_prepare.set()
            first_done = self._wait_for_start_operation("confirm_first_sibling")
            second_done = self._wait_for_start_operation("start_second_sibling")

        self.assertEqual(first_done["state"], "COMMITTED")
        self.assertEqual(second_done["state"], "COMMITTED")
        self.assertEqual(self.fake_adapter.prepare_calls, ["i_image_1", "i_image_2"])
        self.application.cancel_instance(task_id, "i_image_1")
        self.application.cancel_instance(task_id, "i_image_2")

    def test_failed_restart_keeps_the_existing_process_running(self) -> None:
        created = create_task(self.commands, "t_restart_prepare_failure")
        draft = image_plan("t_restart_prepare_failure")
        saved = self.application.save_plan_and_create_instances(
            "t_restart_prepare_failure",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_restart_prepare_failure",
            envelope=envelope("save-restart-prepare-failure", created["revision"]),
        )
        self._configure_runtime_artifact("restart-prepare-failure-agent")
        started = self.application.confirm_and_start_ready_instances(
            "t_restart_prepare_failure",
            operation_id="start_before_restart_failure",
            envelope=envelope(
                "start-before-restart-failure", saved["task_revision"]
            ),
        )
        original_launch = started["launches"][0]["launch"]["launch_id"]
        self.fake_adapter.prepare_error = HarnessError(
            "PROCESS_START_FAILED",
            "The replacement runtime could not be prepared.",
            {"failure_type": "InjectedRestartPrepareFailure"},
        )
        self.application.start_monitoring()

        queued = self.application.restart_instance(
            "t_restart_prepare_failure",
            "i_image_1",
            operation_id="restart_prepare_failure",
            envelope=envelope(
                "restart-prepare-failure",
                self.store.task.revision(
                    "t_restart_prepare_failure", "t_restart_prepare_failure"
                ),
            ),
        )
        self.assertEqual(queued["state"], "QUEUED")
        failed = self._wait_for_start_operation("restart_prepare_failure")

        self.assertEqual(failed["state"], "RETRYABLE_FAILED")
        instance = self.store.instance.get(
            "t_restart_prepare_failure", "i_image_1"
        )
        self.assertEqual(instance["status"], "RUNNING")
        self.assertEqual(instance["process"]["launch_id"], original_launch)
        self.application.cancel_instance("t_restart_prepare_failure", "i_image_1")

    def test_only_latest_legacy_instance_start_is_recovered(self) -> None:
        for operation_id, prepared_at in (
            ("start_legacy_old", "2026-08-28T03:00:00Z"),
            ("start_legacy_new", "2026-08-28T03:01:00Z"),
        ):
            atomic_write_json(
                self.application._intent_path(operation_id),
                {
                    "schema_version": "1.0",
                    "kind": "START_INSTANCE",
                    "operation_id": operation_id,
                    "request_sha256": operation_id.ljust(64, "0"),
                    "request": {
                        "task_id": "t_legacy_instance_start",
                        "instance_id": "i_image_1",
                        "envelope": {},
                    },
                    "state": "PREPARED",
                    "prepared_at": prepared_at,
                    "result": None,
                },
            )

        with patch.object(self.application, "_resume_instance_operation") as resume:
            self.application._run_pending_starts()

        self.assertEqual(
            read_json(self.application._intent_path("start_legacy_old"))["state"],
            "SUPERSEDED",
        )
        resume.assert_called_once_with(
            self.application._intent_path("start_legacy_new")
        )

    def _wait_for_start_operation(
        self, operation_id: str, *, timeout_seconds: float = 5
    ) -> dict:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            operation = self.application.get_start_operation(operation_id)
            if operation["state"] not in {"QUEUED", "RUNNING"}:
                return operation
            time.sleep(0.02)
        self.fail(f"start operation {operation_id} did not finish")

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

    def test_ppt_plan_is_ready_for_runtime_start(self) -> None:
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
        self.assertEqual(result["plan"]["instances"][0]["status"], "READY")
        self.assertNotIn("credential_pair_ref", result["plan"]["instances"][0])

    def test_ppt_start_gate_lists_unfinished_images_and_is_reversible(self) -> None:
        created = create_task(self.commands, "t_ppt_gate")
        draft = image_to_ppt_plan("t_ppt_gate")
        saved = self.application.save_plan_and_create_instances(
            "t_ppt_gate",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_ppt_gate",
            envelope=envelope("save-ppt-gate", created["revision"]),
        )
        confirmed = self.commands.confirm_start(
            "t_ppt_gate",
            envelope("confirm-ppt-gate", saved["task_revision"]),
        )

        with self.assertRaises(HarnessError) as blocked:
            self.application.start_instance(
                "t_ppt_gate",
                "i_ppt_1",
                operation_id="start_blocked_ppt",
                envelope=envelope(
                    "start-blocked-ppt", confirmed["task_revision"]
                ),
            )
        self.assertEqual(blocked.exception.code, "INVALID_STATE_TRANSITION")
        self.assertEqual(
            blocked.exception.details["unfinished_instance_ids"], ["i_image_1"]
        )

        finished = self.commands.set_manual_finished(
            "t_ppt_gate",
            "i_image_1",
            True,
            envelope("finish-ppt-gate-image", confirmed["task_revision"]),
        )
        self.application._require_ppt_start_gate("t_ppt_gate", "i_ppt_1")
        resumed = self.commands.set_manual_finished(
            "t_ppt_gate",
            "i_image_1",
            False,
            envelope("resume-ppt-gate-image", finished["task_revision"]),
        )
        with self.assertRaises(HarnessError):
            self.application._require_ppt_start_gate("t_ppt_gate", "i_ppt_1")
        self.assertFalse(
            next(
                item
                for item in resumed["plan"]["instances"]
                if item["instance_id"] == "i_image_1"
            )["manual_finished"]
        )

    def test_manually_finished_images_authorize_ppt_process_start(self) -> None:
        created = create_task(self.commands, "t_ppt_manual_gate_start")
        draft = image_to_ppt_plan("t_ppt_manual_gate_start")
        saved = self.application.save_plan_and_create_instances(
            "t_ppt_manual_gate_start",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_ppt_manual_gate_start",
            envelope=envelope(
                "save-ppt-manual-gate-start", created["revision"]
            ),
        )
        confirmed = self.commands.confirm_start(
            "t_ppt_manual_gate_start",
            envelope(
                "confirm-ppt-manual-gate-start", saved["task_revision"]
            ),
        )
        finished = self.commands.set_manual_finished(
            "t_ppt_manual_gate_start",
            "i_image_1",
            True,
            envelope(
                "finish-ppt-manual-gate-image", confirmed["task_revision"]
            ),
        )
        self._configure_runtime_artifact("ppt-manual-gate-runtime")
        self.application.start_monitoring()

        queued = self.application.start_instance(
            "t_ppt_manual_gate_start",
            "i_ppt_1",
            operation_id="start_ppt_after_manual_gate",
            envelope=envelope(
                "start-ppt-after-manual-gate", finished["task_revision"]
            ),
        )
        self.assertEqual(queued["state"], "QUEUED")
        completed = self._wait_for_start_operation("start_ppt_after_manual_gate")

        self.assertEqual(completed["state"], "COMMITTED")
        plan = self.store.plan.get(
            "t_ppt_manual_gate_start", "t_ppt_manual_gate_start"
        )
        assert plan is not None
        ppt_instance = next(
            item for item in plan["instances"] if item["instance_id"] == "i_ppt_1"
        )
        ppt_stage = next(
            item for item in plan["stages"] if item["stage_id"] == "s_ppt"
        )
        self.assertEqual(ppt_instance["status"], "RUNNING")
        self.assertEqual(ppt_stage["status"], "RUNNING")
        self.assertIsNotNone(
            ppt_instance["requirement_lifecycle"]["first_activated_at"]
        )
        self.application.cancel_instance("t_ppt_manual_gate_start", "i_ppt_1")

    def test_waiting_approval_task_still_allows_ready_instance_start(self) -> None:
        self._configure_runtime_artifact("waiting-approval-start-fake-agent")
        task_id = "t_waiting_approval_start"
        created = create_task(self.commands, task_id)
        draft = image_plan(task_id, count=2)
        saved = self.application.save_plan_and_create_instances(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_waiting_approval_start",
            envelope=envelope("save-waiting-approval-start", created["revision"]),
        )
        confirmed = self.commands.confirm_start(
            task_id,
            envelope("confirm-waiting-approval-start", saved["task_revision"]),
        )
        starting = self.commands.transition_instance(
            task_id,
            "i_image_1",
            "STARTING",
            envelope("gate-starting", confirmed["task_revision"], "adapter"),
        )
        running = self.commands.transition_instance(
            task_id,
            "i_image_1",
            "RUNNING",
            envelope("gate-running", starting["task_revision"], "adapter"),
        )
        self.assertEqual(
            next(
                item
                for item in running["plan"]["instances"]
                if item["instance_id"] == "i_image_1"
            )["status"],
            "RUNNING",
        )
        self.fake_adapter.observation = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={"job_id": "job_waiting_approval_start"},
        )
        gated = self.application.observe_instance(task_id, "i_image_1")
        self.assertEqual(gated["instance"]["status"], "WAITING_APPROVAL")
        plan = self.store.plan.get(task_id, task_id)
        self.assertEqual(plan["task"]["status"], "WAITING_APPROVAL")

        queued = self.application.start_instance(
            task_id,
            "i_image_2",
            operation_id="start_waiting_approval_sibling",
            envelope=envelope(
                "start-waiting-approval-sibling",
                self.store.task.revision(task_id, task_id),
            ),
        )
        self.assertEqual(queued["state"], "QUEUED")
        completed = self.application._resume_instance_operation(
            self.application._intent_path("start_waiting_approval_sibling")
        )

        self.assertEqual(completed["state"], "COMMITTED")
        self.assertEqual(
            self.store.instance.get(task_id, "i_image_2")["status"],
            "RUNNING",
        )
        self.application.cancel_instance(task_id, "i_image_2")

    def test_prior_ppt_gate_failure_can_be_recovered_without_recreating_task(self) -> None:
        created = create_task(self.commands, "t_recover_ppt_gate")
        draft = image_to_ppt_plan("t_recover_ppt_gate")
        saved = self.application.save_plan_and_create_instances(
            "t_recover_ppt_gate",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_recover_ppt_gate",
            envelope=envelope("save-recover-ppt-gate", created["revision"]),
        )
        confirmed = self.commands.confirm_start(
            "t_recover_ppt_gate",
            envelope("confirm-recover-ppt-gate", saved["task_revision"]),
        )
        finished = self.commands.set_manual_finished(
            "t_recover_ppt_gate",
            "i_image_1",
            True,
            envelope("finish-recover-ppt-gate", confirmed["task_revision"]),
        )
        self._configure_runtime_artifact("recover-ppt-gate-runtime")
        self.application.start_monitoring()
        failed_at = "2026-08-28T07:45:03.051352Z"
        failure = {
            "attempt": 1,
            "code": "INVALID_STATE_TRANSITION",
            "details": {},
            "failed_at": failed_at,
            "message": "The instance stage and task are not authorized to start.",
            "operation_id": "recoverable_prior_ppt_gate_start",
            "phase": "PROCESS_STARTING",
            "retryable": False,
        }
        atomic_write_json(
            self.application._intent_path("recoverable_prior_ppt_gate_start"),
            {
                "schema_version": "1.1",
                "kind": "START_INSTANCE",
                "operation_id": "recoverable_prior_ppt_gate_start",
                "request_sha256": "prior-release-gate-mismatch",
                "request": {
                    "task_id": "t_recover_ppt_gate",
                    "instance_id": "i_ppt_1",
                    "envelope": envelope(
                        "recoverable-prior-ppt-gate-start",
                        finished["task_revision"],
                    ).model_dump(mode="json"),
                },
                "target_instance_ids": ["i_ppt_1"],
                "unavailable": [],
                "instance_progress": {
                    "i_ppt_1": {
                        "state": "ABORTED",
                        "attempt": 1,
                        "launch_id": "launch_prior_gate_mismatch",
                        "side_effect_stage": "NONE",
                        "last_error": deepcopy(failure),
                        "updated_at": failed_at,
                    }
                },
                "state": "ABORTED",
                "last_error": deepcopy(failure),
                "error": deepcopy(failure),
                "created_at": failed_at,
                "updated_at": failed_at,
                "completed_at": failed_at,
                "max_attempts": 3,
                "result": None,
            },
        )

        retried = self.application.retry_start_operation(
            "recoverable_prior_ppt_gate_start",
            envelope=envelope(
                "retry-prior-ppt-gate-start",
                self.store.task.revision(
                    "t_recover_ppt_gate", "t_recover_ppt_gate"
                ),
            ),
        )
        self.assertEqual(retried["state"], "QUEUED")
        completed = self._wait_for_start_operation(
            "recoverable_prior_ppt_gate_start"
        )
        self.assertEqual(completed["state"], "COMMITTED")
        self.application.cancel_instance("t_recover_ppt_gate", "i_ppt_1")

    def test_task_start_targets_only_instances_in_ready_stages(self) -> None:
        created = create_task(self.commands, "t_staged_start")
        draft = image_to_ppt_plan("t_staged_start")
        saved = self.application.save_plan_and_create_instances(
            "t_staged_start",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_staged_start",
            envelope=envelope("save-staged-start", created["revision"]),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_start_intent":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.application.confirm_and_start_ready_instances(
                "t_staged_start",
                operation_id="start_staged_start",
                envelope=envelope("start-staged-start", saved["task_revision"]),
                crash_hook=crash,
            )
        intent = read_json(self.application._intent_path("start_staged_start"))
        self.assertEqual(intent["target_instance_ids"], ["i_image_1"])

    def test_two_ppt_instances_start_on_distinct_ports_and_workspaces(self) -> None:
        self._configure_runtime_artifact("parallel-ppt-fake-agent")
        created = create_task(self.commands, "t_parallel_ppt")
        draft = ppt_plan("t_parallel_ppt", count=2)
        saved = self.application.save_plan_and_create_instances(
            "t_parallel_ppt",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save_parallel_ppt",
            envelope=envelope("save-parallel-ppt", created["revision"]),
        )

        started = self.application.confirm_and_start_ready_instances(
            "t_parallel_ppt",
            operation_id="start_parallel_ppt",
            envelope=envelope("start-parallel-ppt", saved["task_revision"]),
        )

        self.assertEqual(started["state"], "COMMITTED")
        instances = [
            self.store.instance.get("t_parallel_ppt", instance_id)
            for instance_id in ("i_ppt_1", "i_ppt_2")
        ]
        self.assertEqual({item["status"] for item in instances}, {"RUNNING"})
        self.assertEqual(len({item["process"]["port"] for item in instances}), 2)
        roots = [
            self.store.layout.workspace_root
            / "tasks"
            / "t_parallel_ppt"
            / "instances"
            / item["instance_id"]
            for item in instances
        ]
        self.assertEqual(len(set(roots)), 2)
        self.assertTrue(all(root.is_dir() for root in roots))
        for item in instances:
            self.application.cancel_instance("t_parallel_ppt", item["instance_id"])

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

    def test_start_recovery_ignores_cross_task_runtime_settings_sagas(self) -> None:
        self._configure_runtime_artifact("cross-task-settings-recovery-agent")
        for saga_state in ("WAITING_SAFE_POINT", "FAILED"):
            with self.subTest(saga_state=saga_state):
                suffix = saga_state.lower()
                foreign_task_id = f"t_foreign_settings_{suffix}"
                recovery_task_id = f"t_start_recovery_{suffix}"
                foreign_saved = self._save_image_task(foreign_task_id)
                recovery_saved = self._save_image_task(recovery_task_id)

                base = self.runtime_settings.get(foreign_task_id, "i_image_1")
                starting = self.commands.transition_instance(
                    foreign_task_id,
                    "i_image_1",
                    "STARTING",
                    envelope(
                        f"start-foreign-{suffix}",
                        foreign_saved["task_revision"],
                        "adapter",
                    ),
                )
                self.commands.transition_instance(
                    foreign_task_id,
                    "i_image_1",
                    "RUNNING",
                    envelope(
                        f"run-foreign-{suffix}",
                        starting["task_revision"],
                        "adapter",
                    ),
                )
                safe_now = saga_state == "FAILED"
                self.fake_adapter.observation = AdapterObservation(
                    "RUNNING",
                    step_id="candidate_generation",
                    details={
                        "workflow_boundary": {
                            "state": "candidate_generation",
                            "checkpoint_id": "checkpoint_foreign" if safe_now else None,
                            "safe_now": safe_now,
                            "reason": None if safe_now else "ACTIVE_JOB",
                        }
                    },
                )
                proposal = self.runtime_settings.propose(
                    foreign_task_id,
                    "i_image_1",
                    base_revision=base["revision"]["current"],
                    patch={"watermark": True},
                    sync_unstarted_image_work_items=False,
                    expected_sync_instance_ids=[],
                    envelope=envelope(
                        f"propose-foreign-{suffix}",
                        self.store.task.revision(foreign_task_id, foreign_task_id),
                    ),
                )
                if saga_state == "FAILED":
                    with self.assertRaises(HarnessError) as raised:
                        self.runtime_settings.confirm(
                            foreign_task_id,
                            "i_image_1",
                            proposal["proposal_id"],
                            envelope=envelope(
                                f"confirm-foreign-{suffix}",
                                self.store.task.revision(foreign_task_id, foreign_task_id),
                            ),
                        )
                    self.assertEqual(raised.exception.code, "CONTROL_PLANE_NOT_READY")
                else:
                    pending = self.runtime_settings.confirm(
                        foreign_task_id,
                        "i_image_1",
                        proposal["proposal_id"],
                        envelope=envelope(
                            f"confirm-foreign-{suffix}",
                            self.store.task.revision(foreign_task_id, foreign_task_id),
                        ),
                    )
                    self.assertEqual(pending["status"], "WAITING_SAFE_POINT")

                operation_id = f"recover-cross-task-{suffix}"

                def crash(checkpoint: str) -> None:
                    if checkpoint == "after_start_intent":
                        raise SimulatedCrash(checkpoint)

                with self.assertRaises(SimulatedCrash):
                    self.application.confirm_and_start_ready_instances(
                        recovery_task_id,
                        operation_id=operation_id,
                        envelope=envelope(
                            f"start-recovery-{suffix}",
                            recovery_saved["task_revision"],
                        ),
                        crash_hook=crash,
                    )

                recovered = self.application.recover()
                recovery = next(item for item in recovered if item["operation_id"] == operation_id)
                self.assertEqual(recovery["status"], "RECOVERED")
                self.assertEqual(
                    self.store.instance.get(recovery_task_id, "i_image_1")["status"],
                    "RUNNING",
                )
                self.application.cancel_instance(recovery_task_id, "i_image_1")

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

    def test_subset_start_launches_only_selected_instances(self) -> None:
        self._configure_runtime_artifact("subset-start-fake-agent")
        created = create_task(self.commands, "t_subset_start")
        draft = image_plan("t_subset_start", count=2)
        saved = self.application.save_plan_and_create_instances(
            "t_subset_start",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save-subset-start",
            envelope=envelope("save-subset-start", created["revision"]),
        )

        first = self.application.confirm_and_start_ready_instances(
            "t_subset_start",
            operation_id="start-subset-first",
            envelope=envelope("start-subset-first", saved["task_revision"]),
            only_instance_ids=["i_image_1"],
        )

        self.assertEqual(len(first["launches"]), 1)
        self.assertEqual(
            self.store.instance.get("t_subset_start", "i_image_1")["status"],
            "RUNNING",
        )
        self.assertEqual(
            self.store.instance.get("t_subset_start", "i_image_2")["status"],
            "READY",
        )

        second = self.application.confirm_and_start_ready_instances(
            "t_subset_start",
            operation_id="start-subset-second",
            envelope=envelope(
                "start-subset-second",
                self.store.task.revision("t_subset_start", "t_subset_start"),
            ),
            only_instance_ids=["i_image_2"],
        )

        self.assertEqual(len(second["launches"]), 1)
        self.assertEqual(
            self.store.instance.get("t_subset_start", "i_image_2")["status"],
            "RUNNING",
        )
        self.assertEqual(len(self.fake_adapter.start_calls), 2)
        self.application.cancel_instance("t_subset_start", "i_image_1")
        self.application.cancel_instance("t_subset_start", "i_image_2")

    def test_subset_start_rejects_instances_that_are_not_ready(self) -> None:
        self._configure_runtime_artifact("subset-reject-fake-agent")
        created = create_task(self.commands, "t_subset_reject")
        draft = image_plan("t_subset_reject", count=2)
        saved = self.application.save_plan_and_create_instances(
            "t_subset_reject",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id="save-subset-reject",
            envelope=envelope("save-subset-reject", created["revision"]),
        )

        with self.assertRaises(HarnessError) as unknown:
            self.application.confirm_and_start_ready_instances(
                "t_subset_reject",
                operation_id="start-subset-unknown",
                envelope=envelope("start-subset-unknown", saved["task_revision"]),
                only_instance_ids=["i_image_1", "i_unknown"],
            )
        self.assertEqual(unknown.exception.code, "INVALID_STATE_TRANSITION")
        self.assertEqual(
            unknown.exception.details["instance_ids"],
            ["i_unknown"],
        )

        started = self.application.confirm_and_start_ready_instances(
            "t_subset_reject",
            operation_id="start-subset-all",
            envelope=envelope("start-subset-all", saved["task_revision"]),
            only_instance_ids=["i_image_1", "i_image_2"],
        )
        self.assertEqual(len(started["launches"]), 2)

        with self.assertRaises(HarnessError) as already_running:
            self.application.confirm_and_start_ready_instances(
                "t_subset_reject",
                operation_id="start-subset-again",
                envelope=envelope(
                    "start-subset-again",
                    self.store.task.revision("t_subset_reject", "t_subset_reject"),
                ),
                only_instance_ids=["i_image_1"],
            )
        self.assertEqual(already_running.exception.code, "INVALID_STATE_TRANSITION")
        self.application.cancel_instance("t_subset_reject", "i_image_1")
        self.application.cancel_instance("t_subset_reject", "i_image_2")

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
        instance_state = self.store.instance.get("t_agent_start_failure", "i_image_1")
        self.assertEqual(instance_state["status"], "FAILED_TO_START")
        self.assertEqual(instance_state["process"]["state"], "RUNNING")
        self.assertEqual(instance_state["start_failure"]["phase"], "AGENT_STARTING")
        self.assertEqual(
            instance_state["start_failure"]["details"],
            {"http_status": 503, "route": "api/projects/i_image_1/jobs"},
        )
        self.fake_adapter.start_error = None
        with patch.object(
            self.fake_adapter,
            "recover",
            return_value=AdapterRecoveryResult(
                False, "READY", {"mode": "idempotent_start_replay"}
            ),
        ):
            retried = self.application.retry_start_operation(
                "agent-start-failure-operation",
                envelope=envelope(
                    "retry-agent-start-failure",
                    self.store.task.revision(
                        "t_agent_start_failure", "t_agent_start_failure"
                    ),
                ),
            )

        self.assertEqual(retried["state"], "COMMITTED")
        recovered_instance = self.store.instance.get(
            "t_agent_start_failure", "i_image_1"
        )
        self.assertEqual(recovered_instance["status"], "RUNNING")
        self.assertIsNone(recovered_instance.get("start_failure"))
        self.assertEqual(len(self.fake_adapter.start_calls), 1)
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
            ["INSTANCE_SUCCEEDED"],
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
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 1)

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
            envelope=envelope("publish-bundle-main-01", approval["approval_revision"]),
        )

        self.assertEqual(resolved["instance"]["status"], "SUCCEEDED")
        self.assertEqual(resolved["candidate"]["status"], "PUBLISHED")
        assets = self.assets.list_assets(task_id)
        self.assertEqual(
            {(item["manifest"]["role"], item["manifest"]["mime_type"]) for item in assets},
            {("final_artwork", "image/png"), ("design_note", "text/markdown")},
        )
        self.assertEqual(
            {item["manifest"]["relative_path"] for item in assets},
            {
                f"resources/shared/{candidate['bundle_id']}.png",
                f"resources/shared/{candidate['bundle_id']}.md",
            },
        )
        self.assertEqual(
            {item["manifest"]["bundle_id"] for item in assets},
            {candidate["bundle_id"]},
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
            envelope=envelope("publish-bundle-main-01", approval["approval_revision"]),
        )
        self.assertEqual(replay["bundle_manifest"], resolved["bundle_manifest"])
        self.assertEqual(len(self.assets.list_assets(task_id)), 4)

    def test_delivery_read_sweep_reconciles_completed_bundle_source(self) -> None:
        task_id = "t_delivery_read_sweep"
        self._running_image_task(task_id, 1)
        image = b"\x89PNG\r\n\x1a\nswept-final-image"
        note = b"# Swept branch note\n"
        instance_root = self.assets.initialize_instance_workspace(task_id, "i_image_1")
        (instance_root / "outputs" / "swept-image.png").write_bytes(image)
        (instance_root / "outputs" / "swept-note.md").write_bytes(note)
        card = self.store.plan.get(task_id, task_id)["task_cards"][0]
        candidate = {
            "schema_version": "1.0",
            "bundle_id": "bundle_swept_01",
            "task_id": task_id,
            "work_item_id": "work_swept_01",
            "instance_id": "i_image_1",
            "task_card_revision": card["revision"],
            "branch_id": "main",
            "checkpoint_id": "checkpoint_0123456789abcdef01234567",
            "image": {
                "private_relative_path": "instances/i_image_1/outputs/swept-image.png",
                "mime_type": "image/png",
                "size_bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "width": 1920,
                "height": 1080,
            },
            "design_note": {
                "private_relative_path": "instances/i_image_1/outputs/swept-note.md",
                "mime_type": "text/markdown",
                "size_bytes": len(note),
                "sha256": hashlib.sha256(note).hexdigest(),
            },
            "status": "PENDING_CONFIRMATION",
            "created_at": "2026-08-29T05:00:41Z",
            "decided_at": None,
            "actor": None,
            "publication_batch_id": None,
        }
        self.fake_adapter.delivery_bundles = [candidate]
        self.fake_adapter.observation = AdapterObservation(
            "RUNNING", step_id="completed", details={"completed": True}
        )

        self.assertEqual(self.application.list_delivery_bundle_candidates(task_id), [])
        self.application.observe_delivery_sources(task_id)

        candidates = self.application.list_delivery_bundle_candidates(task_id)
        self.assertEqual([item["bundle_id"] for item in candidates], ["bundle_swept_01"])
        self.assertEqual(candidates[0]["status"], "PENDING_CONFIRMATION")
        self.assertEqual(
            self.store.instance.get(task_id, "i_image_1")["status"],
            "WAITING_APPROVAL",
        )
        pending = self.approvals.list_approvals(
            task_id=task_id, instance_id="i_image_1", status="PENDING"
        )
        self.assertEqual([item["kind"] for item in pending], ["DELIVERY_REVIEW"])

        self.application.observe_delivery_sources(task_id)
        self.assertEqual(
            self.application.list_delivery_bundle_candidates(task_id), candidates
        )
        self.assertEqual(
            self.approvals.list_approvals(
                task_id=task_id, instance_id="i_image_1", status="PENDING"
            ),
            pending,
        )

        resolved = self.application.resolve_approval(
            pending[0]["approval_id"],
            decision="APPROVED",
            action="publish_bundle",
            payload={},
            operation_id="publish_swept_bundle_01",
            envelope=envelope(
                "publish-swept-bundle-01",
                self.approvals.get_approval(pending[0]["approval_id"])[
                    "approval_revision"
                ],
            ),
        )
        self.assertEqual(resolved["candidate"]["status"], "PUBLISHED")
        self.assertEqual(
            {item["manifest"]["relative_path"] for item in self.assets.list_assets(task_id)},
            {
                "resources/shared/bundle_swept_01.png",
                "resources/shared/bundle_swept_01.md",
            },
        )
        self.application.observe_delivery_sources(task_id)
        self.assertEqual(
            self.application.list_delivery_bundle_candidates(task_id)[0]["status"],
            "PUBLISHED",
        )

    def test_delivery_read_sweep_throttled_per_task(self) -> None:
        task_id = "t_delivery_sweep_throttle"
        self._running_image_task(task_id, 1)
        image = b"\x89PNG\r\n\x1a\nthrottled-final-image"
        note = b"# Throttled branch note\n"
        instance_root = self.assets.initialize_instance_workspace(task_id, "i_image_1")
        (instance_root / "outputs" / "throttled-image.png").write_bytes(image)
        (instance_root / "outputs" / "throttled-note.md").write_bytes(note)
        card = self.store.plan.get(task_id, task_id)["task_cards"][0]
        candidate = {
            "schema_version": "1.0",
            "bundle_id": "bundle_throttled_01",
            "task_id": task_id,
            "work_item_id": "work_throttled_01",
            "instance_id": "i_image_1",
            "task_card_revision": card["revision"],
            "branch_id": "main",
            "checkpoint_id": "checkpoint_0123456789abcdef01234567",
            "image": {
                "private_relative_path": "instances/i_image_1/outputs/throttled-image.png",
                "mime_type": "image/png",
                "size_bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "width": 1920,
                "height": 1080,
            },
            "design_note": {
                "private_relative_path": "instances/i_image_1/outputs/throttled-note.md",
                "mime_type": "text/markdown",
                "size_bytes": len(note),
                "sha256": hashlib.sha256(note).hexdigest(),
            },
            "status": "PENDING_CONFIRMATION",
            "created_at": "2026-08-29T05:00:41Z",
            "decided_at": None,
            "actor": None,
            "publication_batch_id": None,
        }
        self.fake_adapter.delivery_bundles = [candidate]
        self.fake_adapter.observation = AdapterObservation(
            "RUNNING", step_id="completed", details={"completed": True}
        )
        collections: list[str] = []
        collect = self.fake_adapter.collect_delivery_bundles

        def counting_collect(instance_id: str):
            collections.append(instance_id)
            return collect(instance_id)

        self.fake_adapter.collect_delivery_bundles = counting_collect

        self.application.observe_delivery_sources_throttled(task_id)
        self.assertEqual(len(collections), 1)
        self.assertEqual(
            len(self.application.list_delivery_bundle_candidates(task_id)), 1
        )

        # Bridge-style polls inside the interval must not hit the agent again.
        self.application.observe_delivery_sources_throttled(task_id)
        self.application.observe_delivery_sources_throttled(task_id)
        self.assertEqual(len(collections), 1)

        # Once the interval elapsed the read path tops up reconciliation.
        self.application._delivery_observe_swept_at[task_id] -= 10
        self.application.observe_delivery_sources_throttled(task_id)
        self.assertEqual(len(collections), 2)

    def test_complete_delivery_bundle_targets_one_instance_and_replays_published(self) -> None:
        task_id = "t_delivery_complete_fast_path"
        self._running_image_task(task_id, 2)
        image = b"\x89PNG\r\n\x1a\nfast-path-final-image"
        note = b"# Fast path design note\n"
        instance_root = self.assets.initialize_instance_workspace(task_id, "i_image_1")
        (instance_root / "outputs" / "fast-path-image.png").write_bytes(image)
        (instance_root / "outputs" / "fast-path-note.md").write_bytes(note)
        (instance_root / "outputs" / "alternate-fast-path-image.png").write_bytes(image)
        (instance_root / "outputs" / "alternate-fast-path-note.md").write_bytes(note)
        card = self.store.plan.get(task_id, task_id)["task_cards"][0]
        candidate = {
            "schema_version": "1.0",
            "bundle_id": "bundle_fast_path_01",
            "task_id": task_id,
            "work_item_id": "work_fast_path_01",
            "instance_id": "i_image_1",
            "task_card_revision": card["revision"],
            "branch_id": "main",
            "checkpoint_id": "checkpoint_0123456789abcdef01234567",
            "image": {
                "private_relative_path": "instances/i_image_1/outputs/fast-path-image.png",
                "mime_type": "image/png",
                "size_bytes": len(image),
                "sha256": hashlib.sha256(image).hexdigest(),
                "width": 1920,
                "height": 1080,
            },
            "design_note": {
                "private_relative_path": "instances/i_image_1/outputs/fast-path-note.md",
                "mime_type": "text/markdown",
                "size_bytes": len(note),
                "sha256": hashlib.sha256(note).hexdigest(),
            },
            "status": "PENDING_CONFIRMATION",
            "created_at": "2026-08-30T07:00:00Z",
            "decided_at": None,
            "actor": None,
            "publication_batch_id": None,
        }
        alternate = deepcopy(candidate)
        alternate.update(
            bundle_id="bundle_fast_path_02",
            checkpoint_id="checkpoint_89abcdef0123456701234567",
            created_at="2026-08-30T07:01:00Z",
        )
        alternate["image"]["private_relative_path"] = (
            "instances/i_image_1/outputs/alternate-fast-path-image.png"
        )
        alternate["design_note"]["private_relative_path"] = (
            "instances/i_image_1/outputs/alternate-fast-path-note.md"
        )
        self.fake_adapter.delivery_bundles = [candidate, alternate]
        self.fake_adapter.observation = AdapterObservation(
            "RUNNING", step_id="completed", details={"completed": True}
        )
        collections: list[str] = []
        collect = self.fake_adapter.collect_delivery_bundles

        def counting_collect(instance_id: str):
            collections.append(instance_id)
            return collect(instance_id)

        self.fake_adapter.collect_delivery_bundles = counting_collect

        status = self.application.delivery_bundle_status(
            task_id, "i_image_1", candidate["bundle_id"]
        )
        self.assertEqual(status["status"], "UNKNOWN")
        self.assertEqual(collections, [])

        with patch.object(
            self.application,
            "_commit_delivery_completion_result",
            side_effect=SimulatedCrash("before_complete_result_commit"),
        ), self.assertRaises(SimulatedCrash):
            self.application.complete_delivery_bundle(
                task_id,
                "i_image_1",
                candidate["bundle_id"],
                operation_id="complete_fast_path_01",
                envelope=envelope(
                    "complete-fast-path-01",
                    self.store.task.revision(task_id, task_id),
                ),
            )
        bound_intent = read_json(self.application._intent_path("complete_fast_path_01"))
        self.assertEqual(bound_intent["kind"], "COMPLETE_DELIVERY_BUNDLE")
        self.assertEqual(bound_intent["state"], "BOUND")
        self.assertIsNone(bound_intent["result"])
        self.application.recover()

        result = self.application.complete_delivery_bundle(
            task_id,
            "i_image_1",
            candidate["bundle_id"],
            operation_id="complete_fast_path_01",
            envelope=envelope(
                "complete-fast-path-01",
                self.store.task.revision(task_id, task_id),
            ),
        )

        self.assertEqual(result["status"], "PUBLISHED")
        committed_intent = read_json(
            self.application._intent_path("complete_fast_path_01")
        )
        self.assertEqual(committed_intent["state"], "COMMITTED")
        self.assertEqual(collections, ["i_image_1"])
        self.assertEqual(
            {item["manifest"]["relative_path"] for item in self.assets.list_assets(task_id)},
            {
                "resources/shared/bundle_fast_path_01.png",
                "resources/shared/bundle_fast_path_01.md",
            },
        )

        replay = self.application.complete_delivery_bundle(
            task_id,
            "i_image_1",
            candidate["bundle_id"],
            operation_id="complete_fast_path_01",
            envelope=envelope(
                "complete-fast-path-01",
                self.store.task.revision(task_id, task_id),
            ),
        )
        self.assertEqual(replay["status"], "PUBLISHED")
        self.assertEqual(collections, ["i_image_1"])

        alternate_result = self.application.complete_delivery_bundle(
            task_id,
            "i_image_1",
            alternate["bundle_id"],
            operation_id="complete_fast_path_02",
            envelope=envelope(
                "complete-fast-path-02",
                self.store.task.revision(task_id, task_id),
            ),
        )
        self.assertEqual(alternate_result["status"], "PUBLISHED")

        with self.assertRaises(HarnessError) as different_bundle:
            self.application.complete_delivery_bundle(
                task_id,
                "i_image_1",
                alternate["bundle_id"],
                operation_id="complete_fast_path_01",
                envelope=envelope(
                    "complete-fast-path-01",
                    self.store.task.revision(task_id, task_id),
                ),
            )
        self.assertEqual(different_bundle.exception.code, "IDEMPOTENCY_CONFLICT")

        with self.assertRaises(HarnessError) as different_actor:
            self.application.complete_delivery_bundle(
                task_id,
                "i_image_1",
                candidate["bundle_id"],
                operation_id="complete_fast_path_01",
                envelope=envelope(
                    "complete-fast-path-01",
                    self.store.task.revision(task_id, task_id),
                    actor_id="alternate_human",
                ),
            )
        self.assertEqual(different_actor.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_delivery_read_sweep_skips_agents_without_bundle_collection(self) -> None:
        task_id = "t_delivery_sweep_scope"
        created = create_task(self.commands, task_id, "auto")
        draft = image_to_ppt_plan(task_id)
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

        observed_ppt: list[str] = []
        original_ppt_get_status = self.fake_ppt_adapter.get_status
        self.fake_ppt_adapter.get_status = lambda instance_id: (
            observed_ppt.append(instance_id) or original_ppt_get_status(instance_id)
        )
        self.fake_ppt_adapter.collect_delivery_bundles = None

        self.application.observe_delivery_sources(task_id)

        self.assertEqual(observed_ppt, [])
        self.assertEqual(self.application.list_delivery_bundle_candidates(task_id), [])

    def test_delivery_read_sweep_keeps_read_available_when_observation_fails(self) -> None:
        task_id = "t_delivery_sweep_failure"
        self._running_image_task(task_id, 1)

        def failing_status(instance_id: str):
            raise HarnessError("PROCESS_START_FAILED", "Image Agent is unreachable.")

        self.fake_adapter.get_status = failing_status

        self.application.observe_delivery_sources(task_id)

        self.assertEqual(self.application.list_delivery_bundle_candidates(task_id), [])
        self.assertEqual(
            self.store.instance.get(task_id, "i_image_1")["status"], "RUNNING"
        )

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
        approval = self.application.observe_instance(task_id, "i_image_1")["delivery"]["approval"]

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_bundle_manifest_write":
                raise SimulatedCrash(checkpoint)

        resolve_envelope = envelope("publish-crash-safe-bundle", approval["approval_revision"])
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
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 2)

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

    def test_approval_and_delayed_observation_finish_running(self) -> None:
        task_id = "t_delayed_approval_observation"
        observed = self._waiting_approval(task_id)
        approval = observed["approval"]
        self.fake_adapter.observation_delay = 0.1

        with ThreadPoolExecutor(max_workers=2) as executor:
            delayed_observation = executor.submit(
                self.application.observe_instance, task_id, "i_image_1"
            )
            time.sleep(0.02)
            resolution = executor.submit(
                self.application.resolve_approval,
                approval["approval"]["approval_id"],
                decision="APPROVED",
                action="approve_taskbook",
                payload={},
                operation_id="resolve_delayed_approval_observation",
                envelope=envelope(
                    "resolve-delayed-approval-observation",
                    approval["approval_revision"],
                ),
            )
            delayed_observation.result()
            resolution.result()

        instance = self.store.instance.get(task_id, "i_image_1")
        self.assertEqual(instance["status"], "RUNNING")

    def test_resolved_gate_old_snapshot_cannot_restore_waiting_or_notification(self) -> None:
        task_id = "t_resolved_gate_old_snapshot"
        observed = self._waiting_approval(task_id)
        approval = observed["approval"]
        self.application.resolve_approval(
            approval["approval"]["approval_id"],
            decision="APPROVED",
            action="approve_taskbook",
            payload={},
            operation_id="resolve_old_snapshot_gate",
            envelope=envelope("resolve-old-snapshot-gate", approval["approval_revision"]),
        )

        replay = self.application.observe_instance(task_id, "i_image_1")

        self.assertEqual(replay["instance"]["status"], "RUNNING")
        self.assertTrue(replay["observation"]["stale"])
        self.assertIsNone(replay["approval"])
        self.assertEqual(
            self.approvals.list_approvals(task_id=task_id, status="PENDING"), []
        )
        self.assertEqual(self.approvals.unread_count(owner="human"), 0)

    def test_new_gate_after_resolved_old_gate_creates_fresh_attention(self) -> None:
        task_id = "t_new_gate_after_resolution"
        observed = self._waiting_approval(task_id)
        first = observed["approval"]
        self.application.resolve_approval(
            first["approval"]["approval_id"],
            decision="APPROVED",
            action="approve_taskbook",
            payload={},
            operation_id="resolve_before_new_gate",
            envelope=envelope("resolve-before-new-gate", first["approval_revision"]),
        )
        self.fake_adapter.observation = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={"job_id": "job_genuinely_new_gate"},
        )

        next_gate = self.application.observe_instance(task_id, "i_image_1")

        self.assertEqual(next_gate["instance"]["status"], "WAITING_APPROVAL")
        self.assertNotEqual(
            next_gate["approval"]["approval"]["approval_id"],
            first["approval"]["approval_id"],
        )
        self.assertEqual(next_gate["approval"]["approval"]["status"], "PENDING")
        self.assertEqual(next_gate["approval"]["notification"]["status"], "UNREAD")

    def test_image_adapter_hides_resolved_gate_until_boundary_advances(self) -> None:
        checkpoint = "checkpoint_" + "a" * 24
        next_checkpoint = "checkpoint_" + "b" * 24
        state = {
            "settling_gate": {
                "step_id": "candidate_generation_completed",
                "checkpoint_id": checkpoint,
                "timeline_cursor": 12,
                "advance_job_id": "job_advance",
            }
        }
        old_waiting = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="candidate_generation_completed",
            capabilities=("approve_final",),
            details={
                "job_id": "job_advance",
                "job_status": "succeeded",
                "timeline_cursor": 15,
                "workflow_boundary": {"checkpoint_id": checkpoint},
            },
        )

        settling = ImageAgentAdapter._stabilize_advanced_gate(old_waiting, state)

        self.assertEqual(settling.status, "RUNNING")
        self.assertTrue(settling.details["settling_after_approval"])
        self.assertIn("settling_gate", state)

        active_job = ImageAgentAdapter._stabilize_advanced_gate(
            AdapterObservation(
                "RUNNING",
                details={
                    "phase": "candidate_generation_completed",
                    "workflow_boundary": {"checkpoint_id": checkpoint},
                },
            ),
            state,
        )
        self.assertEqual(active_job.status, "RUNNING")
        self.assertIn("settling_gate", state)

        next_gate = ImageAgentAdapter._stabilize_advanced_gate(
            AdapterObservation(
                "WAITING_APPROVAL",
                step_id="candidate_generation_completed",
                capabilities=("approve_final",),
                details={"workflow_boundary": {"checkpoint_id": next_checkpoint}},
            ),
            state,
        )
        self.assertEqual(next_gate.status, "WAITING_APPROVAL")
        self.assertNotIn("settling_gate", state)

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

    def _save_image_task(self, task_id: str) -> dict:
        created = create_task(self.commands, task_id)
        draft = image_plan(task_id)
        return self.application.save_plan_and_create_instances(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id=f"prepare_{task_id}",
            envelope=envelope(f"prepare-{task_id}", created["revision"]),
        )

    def test_background_observation_tracks_repeated_workbench_gates(self) -> None:
        task_id = "t_background_observation"
        self._running_image_task(task_id, 1)
        self.fake_adapter.observation = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={"job_id": "job_first"},
        )

        self.application.observe_active_instances()

        first = self.approvals.list_approvals(task_id=task_id, status="PENDING")[0]
        plan = self.store.plan.get(task_id, task_id)
        self.assertEqual(plan["task"]["status"], "WAITING_APPROVAL")
        self.assertEqual(self.approvals.unread_count(owner="human"), 1)

        self.fake_adapter.observation = AdapterObservation(
            "RUNNING", step_id="generating", details={"job_id": "job_running"}
        )
        self.application.observe_active_instances()
        self.assertEqual(
            self.approvals.get_approval(first["approval_id"])["approval"]["status"],
            "APPROVED",
        )
        self.assertEqual(self.approvals.list_inbox(owner="human")[0]["status"], "HANDLED")

        self.fake_adapter.observation = AdapterObservation(
            "WAITING_APPROVAL",
            step_id="waiting_human_approval",
            capabilities=("approve_taskbook",),
            details={"job_id": "job_second"},
        )
        self.application.observe_active_instances()
        pending = self.approvals.list_approvals(task_id=task_id, status="PENDING")
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(pending[0]["approval_id"], first["approval_id"])
        self.assertEqual(self.approvals.unread_count(owner="human"), 1)

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
