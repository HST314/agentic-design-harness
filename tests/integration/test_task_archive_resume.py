from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.adapters import AdapterRegistry, AgentWorkState
from harness.core.errors import HarnessError
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
)

from integration.test_application_service import FakeImageAdapter, FakePptAdapter

FAKE_AGENT = Path(__file__).resolve().parents[1] / "fixtures" / "fake_agent_process.py"


class TaskArchiveResumeTests(unittest.TestCase):
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
        self._configure_runtime_artifact("archive-fake-agent")

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

    def _start_running_task(self, task_id: str) -> None:
        created = create_task(self.commands, task_id)
        draft = image_plan(task_id)
        saved = self.application.save_plan_and_create_instances(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            operation_id=f"save_{task_id}",
            envelope=envelope(f"save-{task_id}", created["revision"]),
        )
        self.application.confirm_and_start_ready_instances(
            task_id,
            operation_id=f"start_{task_id}",
            envelope=envelope(f"start-{task_id}", saved["task_revision"]),
        )
        instance = self.store.instance.get(task_id, "i_image_1")
        self.assertEqual(instance["status"], "RUNNING")

    def _task_revision(self, task_id: str) -> int:
        return self.store.task.revision(task_id, task_id)

    def _launches(self, task_id: str, instance_id: str) -> list[dict]:
        records = [
            read_json(path)
            for path in sorted(self.supervisor.launch_root.glob("*.json"))
        ]
        return [
            record
            for record in records
            if record["task_id"] == task_id and record["instance_id"] == instance_id
        ]

    def test_archive_suspends_instances_and_blocks_writes(self) -> None:
        self._start_running_task("t_archive")
        revision = self._task_revision("t_archive")
        result = self.application.archive_task(
            "t_archive",
            operation_id="archive_t_archive",
            envelope=envelope("archive-t-archive", revision),
        )

        self.assertEqual(result["task"]["status"], "ARCHIVED")
        self.assertEqual(result["archive_snapshot"]["pre_archive_status"], "RUNNING")
        self.assertEqual(result["archive_snapshot"]["stopped_instance_ids"], ["i_image_1"])
        instance = self.store.instance.get("t_archive", "i_image_1")
        self.assertEqual(instance["status"], "STOPPED")
        launches = self._launches("t_archive", "i_image_1")
        self.assertEqual(launches[-1]["state"], "EXITED")
        self.assertEqual(launches[-1]["exit_reason"], "SUSPENDED")
        self.assertEqual(self.fake_adapter.stop_calls[0][1], "harness_task_archived")
        navigation = self.store.task_navigation.get("t_archive", "t_archive")
        self.assertIsNotNone(navigation["archived_at"])

        replay = self.application.archive_task(
            "t_archive",
            operation_id="archive_t_archive",
            envelope=envelope("archive-t-archive", revision),
        )
        self.assertEqual(replay, result)
        self.assertEqual(len(self.fake_adapter.stop_calls), 1)

        revision = self._task_revision("t_archive")
        draft = image_plan("t_archive")
        with self.assertRaises(HarnessError) as save_ctx:
            self.application.save_plan_and_create_instances(
                "t_archive",
                stages=draft["stages"],
                instances=draft["instances"],
                task_cards=draft["task_cards"],
                operation_id="save_archived_task",
                envelope=envelope("save-archived-task", revision),
            )
        self.assertEqual(save_ctx.exception.code, "TASK_ARCHIVED")
        with self.assertRaises(HarnessError) as start_ctx:
            self.application.confirm_and_start_ready_instances(
                "t_archive",
                operation_id="start_archived_task",
                envelope=envelope("start-archived-task", revision),
            )
        self.assertEqual(start_ctx.exception.code, "TASK_ARCHIVED")
        with self.assertRaises(HarnessError) as again_ctx:
            self.application.archive_task(
                "t_archive",
                operation_id="archive_t_archive_again",
                envelope=envelope("archive-t-archive-again", revision),
            )
        self.assertEqual(again_ctx.exception.code, "INVALID_STATE_TRANSITION")

    def test_resume_restores_snapshot_and_restarts_instances(self) -> None:
        self._start_running_task("t_archive")
        archived = self.application.archive_task(
            "t_archive",
            operation_id="archive_t_archive",
            envelope=envelope("archive-t-archive", self._task_revision("t_archive")),
        )
        self.assertEqual(archived["task"]["status"], "ARCHIVED")

        resume_revision = self._task_revision("t_archive")
        result = self.application.resume_task(
            "t_archive",
            operation_id="resume_t_archive",
            envelope=envelope("resume-t-archive", resume_revision),
        )

        self.assertEqual(result["restored_status"], "RUNNING")
        self.assertEqual(result["restored_instance_ids"], ["i_image_1"])
        self.assertEqual(result["failed_instances"], [])
        self.assertEqual(result["task"]["status"], "RUNNING")
        instance = self.store.instance.get("t_archive", "i_image_1")
        self.assertIn(instance["status"], {"STARTING", "RUNNING"})
        launches = self._launches("t_archive", "i_image_1")
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[-1]["state"], "RUNNING")
        navigation = self.store.task_navigation.get("t_archive", "t_archive")
        self.assertIsNone(navigation["archived_at"])
        # A resumed instance wakes the process only; no new business job is issued.
        # (the single start call comes from the initial task start).
        self.assertEqual(len(self.fake_adapter.start_calls), 1)

        replay = self.application.resume_task(
            "t_archive",
            operation_id="resume_t_archive",
            envelope=envelope("resume-t-archive", resume_revision),
        )
        self.assertEqual(replay, result)
        self.assertEqual(len(self._launches("t_archive", "i_image_1")), 2)

        with self.assertRaises(HarnessError) as again_ctx:
            self.application.resume_task(
                "t_archive",
                operation_id="resume_t_archive_again",
                envelope=envelope("resume-t-archive-again", self._task_revision("t_archive")),
            )
        self.assertEqual(again_ctx.exception.code, "INVALID_STATE_TRANSITION")

        self.application.cancel_instance("t_archive", "i_image_1")

    def test_archive_drain_timeout_aborts_before_stopping_anything(self) -> None:
        self._start_running_task("t_drain")
        self.fake_adapter.work_state = AgentWorkState.ACTIVE
        revision = self._task_revision("t_drain")

        with self.assertRaises(HarnessError) as drain_ctx:
            self.application.archive_task(
                "t_drain",
                operation_id="archive_t_drain",
                envelope=envelope("archive-t-drain", revision),
                drain_timeout_seconds=1,
            )
        self.assertEqual(drain_ctx.exception.code, "ARCHIVE_DRAIN_TIMEOUT")
        self.assertEqual(
            drain_ctx.exception.details["busy_instance_ids"], ["i_image_1"]
        )
        self.assertEqual(self.fake_adapter.stop_calls, [])
        self.assertFalse(self.fake_adapter.quiesced)
        self.assertEqual(len(self.fake_adapter.unquiesce_calls), 1)
        self.assertEqual(
            self.store.instance.get("t_drain", "i_image_1")["status"], "RUNNING"
        )
        self.assertEqual(self.store.task.get("t_drain", "t_drain")["status"], "RUNNING")
        launches = self._launches("t_drain", "i_image_1")
        self.assertEqual(launches[-1]["state"], "RUNNING")

        # Replaying the aborted operation surfaces the same terminal error.
        with self.assertRaises(HarnessError) as replay_ctx:
            self.application.archive_task(
                "t_drain",
                operation_id="archive_t_drain",
                envelope=envelope("archive-t-drain", revision),
                drain_timeout_seconds=1,
            )
        self.assertEqual(replay_ctx.exception.code, "ARCHIVE_DRAIN_TIMEOUT")

        # Forcing skips the drain even while the agent still reports active work.
        forced = self.application.archive_task(
            "t_drain",
            operation_id="archive_t_drain_force",
            envelope=envelope("archive-t-drain-force", self._task_revision("t_drain")),
            force=True,
        )
        self.assertEqual(forced["task"]["status"], "ARCHIVED")
        self.assertEqual(
            self.store.instance.get("t_drain", "i_image_1")["status"], "STOPPED"
        )
        self.assertEqual(self.fake_adapter.stop_calls[0][1], "harness_task_archived")

    def test_archive_drain_unknown_blocks_safe_archive(self) -> None:
        self._start_running_task("t_unknown")
        self.fake_adapter.work_state = AgentWorkState.UNKNOWN
        revision = self._task_revision("t_unknown")

        with self.assertRaises(HarnessError) as unknown_ctx:
            self.application.archive_task(
                "t_unknown",
                operation_id="archive_t_unknown",
                envelope=envelope("archive-t-unknown", revision),
                drain_timeout_seconds=1,
            )
        self.assertEqual(unknown_ctx.exception.code, "ARCHIVE_DRAIN_UNKNOWN")
        self.assertEqual(
            unknown_ctx.exception.details["unknown_instance_ids"], ["i_image_1"]
        )
        # Nothing was stopped: an unproven in-flight state is never idle.
        self.assertEqual(self.fake_adapter.stop_calls, [])
        self.assertFalse(self.fake_adapter.quiesced)
        self.assertEqual(
            self.store.instance.get("t_unknown", "i_image_1")["status"], "RUNNING"
        )
        self.assertEqual(self.store.task.get("t_unknown", "t_unknown")["status"], "RUNNING")

        # A failed probe (adapter reporting unavailable) is equally UNKNOWN.
        self.fake_adapter.work_state = AgentWorkState.IDLE
        self.fake_adapter.available = False
        try:
            with self.assertRaises(HarnessError) as unavailable_ctx:
                self.application.archive_task(
                    "t_unknown",
                    operation_id="archive_t_unknown_adapter",
                    envelope=envelope(
                        "archive-t-unknown-adapter", self._task_revision("t_unknown")
                    ),
                    drain_timeout_seconds=1,
                )
            self.assertEqual(unavailable_ctx.exception.code, "ARCHIVE_DRAIN_UNKNOWN")
            self.assertEqual(self.fake_adapter.stop_calls, [])
        finally:
            self.fake_adapter.available = True

        # Force is the explicit escape hatch for an unproven drain.
        forced = self.application.archive_task(
            "t_unknown",
            operation_id="archive_t_unknown_force",
            envelope=envelope("archive-t-unknown-force", self._task_revision("t_unknown")),
            force=True,
        )
        self.assertEqual(forced["task"]["status"], "ARCHIVED")
        self.assertEqual(
            self.store.instance.get("t_unknown", "i_image_1")["status"], "STOPPED"
        )

    def test_archive_quiesces_before_final_idle_and_rejects_concurrent_submit(self) -> None:
        self._start_running_task("t_quiesce_race")

        def after_drain(point: str) -> None:
            if point != "after_archive_drain":
                return
            self.assertTrue(self.fake_adapter.quiesced)
            with self.assertRaises(HarnessError) as submit_ctx:
                self.fake_adapter.submit_concurrent_work()
            self.assertEqual(submit_ctx.exception.code, "AGENT_QUIESCED")

        archived = self.application.archive_task(
            "t_quiesce_race",
            operation_id="archive_t_quiesce_race",
            envelope=envelope(
                "archive-t-quiesce-race", self._task_revision("t_quiesce_race")
            ),
            crash_hook=after_drain,
        )

        self.assertEqual(archived["task"]["status"], "ARCHIVED")
        self.assertTrue(self.fake_adapter.quiesced)
        self.assertEqual(len(self.fake_adapter.quiesce_calls), 1)

    def test_cancel_task_blocked_while_archived(self) -> None:
        self._start_running_task("t_cancel_guard")
        self.application.archive_task(
            "t_cancel_guard",
            operation_id="archive_t_cancel_guard",
            envelope=envelope(
                "archive-t-cancel-guard", self._task_revision("t_cancel_guard")
            ),
        )

        with self.assertRaises(HarnessError) as cancel_ctx:
            self.application.cancel_task(
                "t_cancel_guard",
                operation_id="cancel_t_cancel_guard",
                envelope=envelope(
                    "cancel-t-cancel-guard", self._task_revision("t_cancel_guard")
                ),
            )
        self.assertEqual(cancel_ctx.exception.code, "TASK_ARCHIVED")
        # The archive snapshot is untouched: the task and its instances stay
        # resumable instead of being permanently cancelled.
        self.assertEqual(
            self.store.task.get("t_cancel_guard", "t_cancel_guard")["status"], "ARCHIVED"
        )
        self.assertEqual(
            self.store.instance.get("t_cancel_guard", "i_image_1")["status"], "STOPPED"
        )

        resumed = self.application.resume_task(
            "t_cancel_guard",
            operation_id="resume_t_cancel_guard",
            envelope=envelope(
                "resume-t-cancel-guard", self._task_revision("t_cancel_guard")
            ),
        )
        self.assertEqual(resumed["task"]["status"], "RUNNING")
        self.assertEqual(resumed["restored_instance_ids"], ["i_image_1"])
        self.application.cancel_instance("t_cancel_guard", "i_image_1")

    def _archive_and_resume(self, task_id: str) -> dict:
        archived = self.application.archive_task(
            task_id,
            operation_id=f"archive_{task_id}",
            envelope=envelope(f"archive-{task_id}", self._task_revision(task_id)),
        )
        self.assertEqual(archived["task"]["status"], "ARCHIVED")
        return self.application.resume_task(
            task_id,
            operation_id=f"resume_{task_id}",
            envelope=envelope(f"resume-{task_id}", self._task_revision(task_id)),
        )

    def test_resume_restores_waiting_approval_snapshot(self) -> None:
        self._start_running_task("t_wait")
        self.commands.transition_instance(
            "t_wait",
            "i_image_1",
            "WAITING_APPROVAL",
            envelope("wait-t-wait", self._task_revision("t_wait")),
        )
        self.assertEqual(
            self.store.task.get("t_wait", "t_wait")["status"], "WAITING_APPROVAL"
        )

        result = self._archive_and_resume("t_wait")

        self.assertEqual(result["restored_status"], "WAITING_APPROVAL")
        self.assertEqual(result["restored_instance_ids"], ["i_image_1"])
        self.assertEqual(result["failed_instances"], [])
        instance = self.store.instance.get("t_wait", "i_image_1")
        self.assertEqual(instance["status"], "WAITING_APPROVAL")
        self.assertEqual(instance["process"]["state"], "RUNNING")
        self.assertEqual(
            self.store.task.get("t_wait", "t_wait")["status"], "WAITING_APPROVAL"
        )
        self.application.cancel_instance("t_wait", "i_image_1")

    def test_resume_restores_failed_snapshot(self) -> None:
        self._start_running_task("t_failed")
        self.commands.transition_instance(
            "t_failed",
            "i_image_1",
            "FAILED",
            envelope("fail-t-failed", self._task_revision("t_failed")),
        )
        self.assertEqual(self.store.task.get("t_failed", "t_failed")["status"], "FAILED")

        result = self._archive_and_resume("t_failed")

        self.assertEqual(result["restored_status"], "FAILED")
        self.assertEqual(result["restored_instance_ids"], ["i_image_1"])
        self.assertEqual(result["failed_instances"], [])
        instance = self.store.instance.get("t_failed", "i_image_1")
        self.assertEqual(instance["status"], "FAILED")
        self.assertEqual(instance["process"]["state"], "RUNNING")
        self.assertEqual(self.store.task.get("t_failed", "t_failed")["status"], "FAILED")
        self.application.cancel_instance("t_failed", "i_image_1")

    def test_resume_restores_succeeded_snapshot(self) -> None:
        self._start_running_task("t_succeeded")
        self.commands.transition_instance(
            "t_succeeded",
            "i_image_1",
            "SUCCEEDED",
            envelope("succeed-t-succeeded", self._task_revision("t_succeeded")),
        )
        task = self.store.task.get("t_succeeded", "t_succeeded")
        self.assertEqual(task["status"], "SUCCEEDED")

        result = self._archive_and_resume("t_succeeded")

        self.assertEqual(result["restored_status"], "SUCCEEDED")
        self.assertEqual(result["restored_instance_ids"], ["i_image_1"])
        self.assertEqual(result["failed_instances"], [])
        instance = self.store.instance.get("t_succeeded", "i_image_1")
        self.assertEqual(instance["status"], "SUCCEEDED")
        self.assertEqual(instance["process"]["state"], "RUNNING")
        stage = self.store.stage.get("t_succeeded", "s_image")
        self.assertEqual(stage["status"], "SUCCEEDED")
        self.assertEqual(
            self.store.task.get("t_succeeded", "t_succeeded")["status"], "SUCCEEDED"
        )
        # A SUCCEEDED instance cannot be cancelled; park it to release the process.
        self.supervisor.suspend_instance("t_succeeded", "i_image_1")

    def test_resume_start_failure_marks_instance_failed_and_replay_is_stable(self) -> None:
        self._start_running_task("t_resume_failure")
        self.application.archive_task(
            "t_resume_failure",
            operation_id="archive_t_resume_failure",
            envelope=envelope(
                "archive-t-resume-failure", self._task_revision("t_resume_failure")
            ),
        )
        revision = self._task_revision("t_resume_failure")
        original_spec = self.fake_adapter.runtime_spec
        self.assertIsNotNone(original_spec)
        self.fake_adapter.runtime_spec = replace(
            original_spec,
            public_environment={
                **original_spec.public_environment,
                "FAKE_HEALTHY": "0",
            },
        )
        try:
            result = self.application.resume_task(
                "t_resume_failure",
                operation_id="resume_t_resume_failure",
                envelope=envelope("resume-t-resume-failure", revision),
            )
        finally:
            self.fake_adapter.runtime_spec = original_spec

        self.assertEqual(result["task"]["status"], "FAILED")
        self.assertEqual(result["restored_instance_ids"], [])
        self.assertEqual(result["failed_instances"][0]["code"], "PROCESS_START_FAILED")
        instance = self.store.instance.get("t_resume_failure", "i_image_1")
        self.assertEqual(instance["status"], "FAILED")
        self.assertEqual(instance["process"]["state"], "EXITED")
        self.assertFalse(self.fake_adapter.quiesced)

        replay = self.application.resume_task(
            "t_resume_failure",
            operation_id="resume_t_resume_failure",
            envelope=envelope("resume-t-resume-failure", revision),
        )
        self.assertEqual(replay, result)

        restarted = self.application.restart_instance(
            "t_resume_failure",
            "i_image_1",
            operation_id="restart_t_resume_failure",
            envelope=envelope(
                "restart-t-resume-failure", self._task_revision("t_resume_failure")
            ),
        )
        self.assertEqual(restarted["state"], "QUEUED")

    def test_resume_unquiesce_failure_recloses_admission_and_stops_launch(self) -> None:
        self._start_running_task("t_unquiesce_failure")
        self.application.archive_task(
            "t_unquiesce_failure",
            operation_id="archive_t_unquiesce_failure",
            envelope=envelope(
                "archive-t-unquiesce-failure",
                self._task_revision("t_unquiesce_failure"),
            ),
        )
        revision = self._task_revision("t_unquiesce_failure")
        original_unquiesce = self.fake_adapter.unquiesce

        def fail_after_reopening(instance_id: str, operation_id: str) -> None:
            original_unquiesce(instance_id, operation_id)
            raise RuntimeError("work admission verification failed")

        self.fake_adapter.unquiesce = fail_after_reopening
        try:
            result = self.application.resume_task(
                "t_unquiesce_failure",
                operation_id="resume_t_unquiesce_failure",
                envelope=envelope("resume-t-unquiesce-failure", revision),
            )
        finally:
            self.fake_adapter.unquiesce = original_unquiesce

        self.assertEqual(result["task"]["status"], "FAILED")
        self.assertEqual(result["restored_instance_ids"], [])
        self.assertEqual(result["failed_instances"][0]["code"], "PROCESS_START_FAILED")
        instance = self.store.instance.get("t_unquiesce_failure", "i_image_1")
        self.assertEqual(instance["status"], "FAILED")
        self.assertEqual(instance["process"]["state"], "EXITED")
        launches = self._launches("t_unquiesce_failure", "i_image_1")
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[-1]["state"], "EXITED")
        self.assertEqual(launches[-1]["exit_reason"], "SUSPENDED")
        self.assertTrue(self.fake_adapter.quiesced)

        replay = self.application.resume_task(
            "t_unquiesce_failure",
            operation_id="resume_t_unquiesce_failure",
            envelope=envelope("resume-t-unquiesce-failure", revision),
        )
        self.assertEqual(replay, result)
        self.assertEqual(
            len(self._launches("t_unquiesce_failure", "i_image_1")), 2
        )


if __name__ == "__main__":
    unittest.main()
