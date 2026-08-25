from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from harness.core.errors import HarnessError, SimulatedCrash
from harness.services.agent_config_materialization import ImageAgentConfigMaterializer
from harness.services.task_config import TaskConfigService
from harness.storage.instance_config_revisions import InstanceConfigRevisionStore
from harness.storage.task_config_revisions import TaskConfigRevisionStore
from runtime_helpers import build_config_snapshot, build_service, create_task

CREATED_AT = "2026-08-25T03:00:00Z"


class ConfigRevisionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        create_task(self.commands, "task_config_recovery")
        self.task_revisions = TaskConfigRevisionStore(self.store)
        self.instance_revisions = InstanceConfigRevisionStore(self.store)

    def tearDown(self) -> None:
        self.store.close()
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_symlink():
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)
        self.temporary.cleanup()

    def test_legacy_task_and_instance_configurations_remain_readable_without_rewrite(self) -> None:
        snapshot = build_config_snapshot()
        task_config = TaskConfigService(self.store, snapshot)
        legacy_task = task_config.pin("task_config_recovery")
        materializer = ImageAgentConfigMaterializer(self.store, task_config)
        materializer.materialize("task_config_recovery", "instance_config_recovery")

        task_current = self.task_revisions.read_current("task_config_recovery")
        instance_current = self.instance_revisions.read_current(
            "task_config_recovery", "instance_config_recovery"
        )

        assert task_current is not None
        assert instance_current is not None
        self.assertTrue(task_current["legacy"])
        self.assertEqual(task_current["revision"]["revision_id"], "task-config-r000001")
        self.assertEqual(task_current["revision"]["provider_ids"], ["ark"])
        self.assertNotIn("providers", task_current["revision"])
        self.assertEqual(
            task_config.get_public("task_config_recovery")["config_hash"],
            legacy_task["config_hash"],
        )
        self.assertTrue(instance_current["legacy"])
        self.assertEqual(
            instance_current["manifest"]["revision_id"], "cfg-inst-r000001"
        )
        self.assertEqual(instance_current["manifest"]["apply_status"], "APPLIED")
        self.assertFalse(self._task_state_path().exists())
        self.assertFalse(self._instance_state_path().exists())

    def test_task_revision_crash_before_pointer_is_recoverable_and_idempotent(self) -> None:
        revision, state = self._task_records()

        with self.assertRaises(SimulatedCrash):
            self.task_revisions.commit(
                "task_config_recovery",
                revision,
                state,
                expected_revision=0,
                crash_hook=self._crash_at("after_task_revision_published"),
            )

        self.assertIsNone(self.task_revisions.read_current("task_config_recovery"))
        recovery = self.task_revisions.recover("task_config_recovery")
        self.assertEqual(recovery["unreferenced_revision_ids"], ["task-config-r000001"])

        committed = self.task_revisions.commit(
            "task_config_recovery",
            revision,
            state,
            expected_revision=0,
        )
        self.assertEqual(committed["state"]["revision"], 1)
        self.assertEqual(
            self.task_revisions.read_current("task_config_recovery")["revision"],
            revision,
        )

    def test_task_pointer_survives_crash_after_atomic_publication(self) -> None:
        revision, state = self._task_records()
        with self.assertRaises(SimulatedCrash):
            self.task_revisions.commit(
                "task_config_recovery",
                revision,
                state,
                expected_revision=0,
                crash_hook=self._crash_at("after_task_state_published"),
            )

        current = self.task_revisions.read_current("task_config_recovery")
        assert current is not None
        self.assertEqual(current["state"], state)
        self.assertEqual(current["revision"], revision)

    def test_incomplete_instance_revision_directory_is_removed_on_recovery(self) -> None:
        manifest, runtime, model_config = self._instance_bundle()
        with self.assertRaises(SimulatedCrash):
            self.instance_revisions.write_revision(
                "task_config_recovery",
                "instance_config_recovery",
                manifest,
                runtime,
                model_config,
                crash_hook=self._crash_at("after_revision_staged"),
            )

        revisions = self._instance_runtime_root() / "revisions"
        self.assertFalse((revisions / "cfg-inst-r000001").exists())
        self.assertTrue(list(revisions.glob(".*.tmp")))
        recovery = self.instance_revisions.recover(
            "task_config_recovery", "instance_config_recovery"
        )
        self.assertTrue(recovery["removed_temporary_paths"])
        self.assertEqual(list(revisions.glob(".*.tmp")), [])

        self.instance_revisions.write_revision(
            "task_config_recovery",
            "instance_config_recovery",
            manifest,
            runtime,
            model_config,
        )
        state = self.instance_revisions.set_current(
            "task_config_recovery",
            "instance_config_recovery",
            "cfg-inst-r000001",
            expected_revision=0,
            updated_at=CREATED_AT,
        )
        self.assertEqual(state["revision"], 1)

    def test_published_instance_revision_and_pointer_are_idempotent_after_crashes(self) -> None:
        manifest, runtime, model_config = self._instance_bundle()
        with self.assertRaises(SimulatedCrash):
            self.instance_revisions.write_revision(
                "task_config_recovery",
                "instance_config_recovery",
                manifest,
                runtime,
                model_config,
                crash_hook=self._crash_at("after_revision_published"),
            )

        self.assertIsNone(
            self.instance_revisions.read_current(
                "task_config_recovery", "instance_config_recovery"
            )
        )
        self.instance_revisions.write_revision(
            "task_config_recovery",
            "instance_config_recovery",
            manifest,
            runtime,
            model_config,
        )
        changed_runtime = {**runtime, "candidate_concurrency": 4}
        changed_manifest = self.instance_revisions.build_manifest(
            **{
                **self._manifest_arguments(manifest),
                "runtime": changed_runtime,
                "model_config": model_config,
            }
        )
        with self.assertRaises(HarnessError) as collision:
            self.instance_revisions.write_revision(
                "task_config_recovery",
                "instance_config_recovery",
                changed_manifest,
                changed_runtime,
                model_config,
            )
        self.assertEqual(collision.exception.code, "SETTINGS_REVISION_CONFLICT")
        with self.assertRaises(SimulatedCrash):
            self.instance_revisions.set_current(
                "task_config_recovery",
                "instance_config_recovery",
                "cfg-inst-r000001",
                expected_revision=0,
                updated_at=CREATED_AT,
                crash_hook=self._crash_at("after_instance_state_published"),
            )

        current = self.instance_revisions.read_current(
            "task_config_recovery", "instance_config_recovery"
        )
        assert current is not None
        self.assertEqual(current["manifest"], manifest)
        self.assertEqual(current["state"]["revision"], 1)

    def test_active_instance_revision_fails_closed_after_content_tampering(self) -> None:
        manifest, runtime, model_config = self._instance_bundle()
        self.instance_revisions.write_revision(
            "task_config_recovery",
            "instance_config_recovery",
            manifest,
            runtime,
            model_config,
        )
        self.instance_revisions.set_current(
            "task_config_recovery",
            "instance_config_recovery",
            "cfg-inst-r000001",
            expected_revision=0,
            updated_at=CREATED_AT,
        )
        runtime_path = (
            self._instance_runtime_root()
            / "revisions"
            / "cfg-inst-r000001"
            / "runtime.yaml"
        )
        runtime_path.chmod(0o600)
        runtime_path.write_text("candidate_concurrency: 1\n", encoding="utf-8")

        with self.assertRaises(HarnessError) as captured:
            self.instance_revisions.read_current(
                "task_config_recovery", "instance_config_recovery"
            )
        self.assertEqual(captured.exception.code, "CONFIG_INTEGRITY_FAILED")

    def test_new_revision_files_reject_provider_urls_and_absolute_paths(self) -> None:
        revision, state = self._task_records()
        revision["runtime"]["service_root"] = "https://provider.invalid"
        revision["config_hash"] = self.task_revisions.build_revision(
            task_id="task_config_recovery",
            revision_id="task-config-r000001",
            parent_revision_id=None,
            source_system_revision=revision["source_system_revision"],
            provider_ids=revision["provider_ids"],
            model_list=revision["model_list"],
            runtime=revision["runtime"],
            created_by=revision["created_by"],
            created_at=revision["created_at"],
        )["config_hash"]
        with self.assertRaises(HarnessError) as task_error:
            self.task_revisions.commit(
                "task_config_recovery", revision, state, expected_revision=0
            )
        self.assertEqual(task_error.exception.code, "CONFIG_INTEGRITY_FAILED")

        manifest, runtime, model_config = self._instance_bundle()
        runtime["style_library_root"] = "/private/library"
        manifest = self.instance_revisions.build_manifest(
            **{
                **self._manifest_arguments(manifest),
                "runtime": runtime,
                "model_config": model_config,
            }
        )
        with self.assertRaises(HarnessError) as instance_error:
            self.instance_revisions.write_revision(
                "task_config_recovery",
                "instance_config_recovery",
                manifest,
                runtime,
                model_config,
            )
        self.assertEqual(instance_error.exception.code, "CONFIG_INTEGRITY_FAILED")

    def _task_records(self) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = build_config_snapshot()
        revision = self.task_revisions.build_revision(
            task_id="task_config_recovery",
            revision_id="task-config-r000001",
            parent_revision_id=None,
            source_system_revision=snapshot.revision,
            provider_ids=sorted(snapshot.providers.providers),
            model_list=snapshot.model_list.model_dump(mode="json"),
            runtime=snapshot.runtime.model_dump(mode="json"),
            created_by={"type": "system", "id": "recovery_test"},
            created_at=CREATED_AT,
        )
        state = {
            "schema_version": "2.0",
            "task_id": "task_config_recovery",
            "current_revision_id": revision["revision_id"],
            "source_system_revision": revision["source_system_revision"],
            "locked_at": None,
            "locked_reason": None,
            "revision": 1,
            "created_at": CREATED_AT,
            "updated_at": CREATED_AT,
        }
        return revision, state

    def _instance_bundle(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        runtime = {
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
                "release": "manual",
            },
        }
        bindings = {
            "intake_clarify": "text-model",
            "confirmation_build": "text-model",
            "initial_candidate_generation": "image-model",
            "self_check_inspection": "vision-model",
            "self_check_rework": "image-model",
            "human_prompt_rework": "image-model",
        }
        model_config = {
            "model_config_id": "runtime-config-test",
            "state_bindings": [
                {"state": state, "model": model}
                for state, model in bindings.items()
            ],
        }
        effective_runtime = {
            **runtime,
            "self_check": {
                key: value
                for key, value in runtime["self_check"].items()
                if key != "release"
            },
        }
        manifest = self.instance_revisions.build_manifest(
            task_id="task_config_recovery",
            instance_id="instance_config_recovery",
            revision_id="cfg-inst-r000001",
            parent_revision_id=None,
            task_config_revision_id="task-config-r000001",
            overrides={},
            effective_runtime=effective_runtime,
            model_bindings=bindings,
            runtime=runtime,
            model_config=model_config,
            created_by={"type": "system", "id": "recovery_test"},
            created_at=CREATED_AT,
            confirmed_at=CREATED_AT,
            apply_mode="before_start",
            apply_status="APPLIED",
            effective_from_state="initial",
        )
        return manifest, runtime, model_config

    @staticmethod
    def _manifest_arguments(manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            key: manifest[key]
            for key in (
                "task_id",
                "instance_id",
                "revision_id",
                "parent_revision_id",
                "task_config_revision_id",
                "overrides",
                "effective_runtime",
                "model_bindings",
                "created_by",
                "created_at",
                "confirmed_at",
                "apply_mode",
                "apply_status",
                "branch_id",
                "checkpoint_id",
                "effective_from_state",
            )
        }

    @staticmethod
    def _crash_at(target: str):
        def crash(checkpoint: str) -> None:
            if checkpoint == target:
                raise SimulatedCrash(checkpoint)

        return crash

    def _task_state_path(self) -> Path:
        return (
            self.store.layout.control_root
            / "tasks"
            / "task_config_recovery"
            / "master"
            / "config"
            / "state.json"
        )

    def _instance_runtime_root(self) -> Path:
        return (
            self.store.layout.workspace_root
            / "tasks"
            / "task_config_recovery"
            / "instances"
            / "instance_config_recovery"
            / "runtime-config"
        )

    def _instance_state_path(self) -> Path:
        return self._instance_runtime_root() / "state.json"


if __name__ == "__main__":
    unittest.main()
