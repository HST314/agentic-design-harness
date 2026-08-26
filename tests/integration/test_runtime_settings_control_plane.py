from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from harness.adapters import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    AdapterRegistry,
    ValidationResult,
)
from harness.api.app import create_app
from harness.core.config import HarnessSettings
from harness.core.errors import HarnessError, SimulatedCrash
from harness.domain.commands import CommandEnvelope
from harness.services.agent_config_materialization import ImageAgentConfigMaterializer
from harness.services.instance_runtime_settings import InstanceRuntimeSettingsService
from harness.services.runtime_config_observability import RuntimeConfigObservability
from harness.services.task_config import TaskConfigService
from harness.services.task_config_rebase import TaskConfigRebaseService
from runtime_helpers import (
    build_config_snapshot,
    build_service,
    create_task,
    envelope,
    image_plan,
)

ROOT = Path(__file__).resolve().parents[2]


class SafePointImageAdapter:
    agent_type = "image"
    available = True

    def __init__(self) -> None:
        self.boundary = {
            "state": "candidate_review",
            "checkpoint_id": "checkpoint_safe_1",
            "safe_now": True,
            "reason": None,
        }
        self.receipts: dict[str, dict] = {}
        self.apply_calls = 0
        self.receipt_from_checkpoint_override: str | None = None

    def get_status(self, instance_id: str) -> AdapterObservation:
        return AdapterObservation(
            "RUNNING",
            step_id=str(self.boundary["state"]),
            details={"workflow_boundary": deepcopy(self.boundary)},
        )

    def apply_runtime_revision(
        self,
        instance_id: str,
        *,
        revision_id: str,
        from_checkpoint: str,
        expected_config_hash: str,
        effective_from_state: str,
        idempotency_key: str,
    ) -> dict:
        self.apply_calls += 1
        receipt = self.receipts.setdefault(
            idempotency_key,
            {
                "revision_id": revision_id,
                "branch_id": "branch_config_1",
                "checkpoint_id": "checkpoint_config_1",
                "from_checkpoint": self.receipt_from_checkpoint_override or from_checkpoint,
                "effective_from_state": effective_from_state,
                "config_hash": expected_config_hash,
            },
        )
        return deepcopy(receipt)

    def validate_task_card(self, card) -> ValidationResult:
        return ValidationResult(True)

    def prepare(self, request):
        raise AssertionError("not used")

    def start(self, instance_id: str, operation_id: str) -> AdapterCommandResult:
        return AdapterCommandResult(True, operation_id)

    def stop(self, instance_id: str, reason: str, operation_id: str):
        return AdapterCommandResult(True, operation_id)

    def request_advance(self, instance_id: str, action: str, payload: dict, operation_id: str):
        return AdapterCommandResult(True, operation_id)

    def collect_deliveries(self, instance_id: str) -> list:
        return []

    def collect_usage(self, instance_id: str, cursor: str | None) -> list:
        return []

    def get_ui_url(self, instance_id: str) -> None:
        return None

    def validate_ui_url(self, instance, ui_url: str) -> ValidationResult:
        return ValidationResult(False)

    def recover(self, instance_snapshot) -> AdapterRecoveryResult:
        return AdapterRecoveryResult(True, "RUNNING")


class RuntimeSettingsControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        self.task_config = TaskConfigService(self.store, build_config_snapshot())
        self.materializer = ImageAgentConfigMaterializer(self.store, self.task_config)
        self.adapter = SafePointImageAdapter()
        self.adapters = AdapterRegistry([self.adapter])
        self.observability = RuntimeConfigObservability(self.store)
        self.settings = InstanceRuntimeSettingsService(
            self.store,
            self.task_config,
            self.materializer,
            self.adapters,
            self.observability,
        )
        self.rebase = TaskConfigRebaseService(
            self.store,
            self.task_config,
            self.materializer,
            self.observability,
        )

    def tearDown(self) -> None:
        self.store.close()
        for path in sorted(self.root.rglob("*"), reverse=True):
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        self.temporary.cleanup()

    def test_proposal_confirmation_applies_exact_unstarted_sync_scope(self) -> None:
        task_id = self._planned_task("task_sync_settings", count=2)
        source = self.settings.get(task_id, "i_image_1")
        self.settings.get(task_id, "i_image_2")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=source["revision"]["current"],
            patch={
                "category_constraint": {"release": "manual"},
                "style_direction": {"release": "off"},
                "candidate_concurrency": 2,
                "watermark": True,
            },
            sync_unstarted_image_work_items=True,
            expected_sync_instance_ids=["i_image_2"],
            envelope=self._settings_envelope(task_id, "propose-sync"),
        )

        result = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-sync"),
        )

        self.assertEqual(result["status"], "APPLIED_BEFORE_START")
        self.assertEqual(
            [item["field"] for item in proposal["diff"]],
            [
                "category_constraint.release",
                "style_direction.release",
                "candidate_concurrency",
                "watermark",
            ],
        )
        self.assertEqual(result["sync_instance_ids"], ["i_image_2"])
        self.assertTrue(source["scope"]["work_item_id"].startswith("work_"))
        self.assertNotEqual(source["scope"]["work_item_id"], "s_image")
        for instance_id in ("i_image_1", "i_image_2"):
            current = self.materializer.revisions.read_current(task_id, instance_id)
            self.assertEqual(current["manifest"]["overrides"]["candidate_concurrency"], 2)
            self.assertEqual(
                current["manifest"]["overrides"]["category_constraint"]["release"],
                "manual",
            )
            self.assertEqual(
                current["manifest"]["overrides"]["style_direction"]["release"],
                "off",
            )
            self.assertTrue(current["manifest"]["overrides"]["watermark"])
            self.assertEqual(current["manifest"]["apply_status"], "APPLIED")

    def test_runtime_settings_report_catalog_model_ids_without_false_diff(self) -> None:
        task_id = self._planned_task("task_model_id_projection")
        current = self.settings.get(task_id, "i_image_1")

        models = current["values"]["advanced_model_overrides"]
        self.assertEqual(models["inherited"]["intake_clarify"], "ark-text-primary")
        self.assertEqual(models["effective"], models["inherited"])
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=current["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-model-projection"),
        )
        self.assertEqual([item["field"] for item in proposal["diff"]], ["watermark"])

    def test_confirm_rejects_same_idempotency_key_for_a_different_request(self) -> None:
        task_id = self._planned_task("task_confirm_idempotency")
        current = self.settings.get(task_id, "i_image_1")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=current["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-confirm-idempotency"),
        )
        first = self._settings_envelope(task_id, "confirm-idempotency")
        self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=first,
        )

        with self.assertRaises(HarnessError) as raised:
            self.settings.confirm(
                task_id,
                "i_image_1",
                proposal["proposal_id"],
                envelope=envelope(
                    first.idempotency_key,
                    first.expected_revision,
                    actor_id="another-actor",
                ),
            )
        self.assertEqual(raised.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_persisted_sync_intent_blocks_conflicting_target_proposals(self) -> None:
        task_id = self._planned_task("task_sync_intent_gate", count=2)
        source = self.settings.get(task_id, "i_image_1")
        self.settings.get(task_id, "i_image_2")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=source["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=True,
            expected_sync_instance_ids=["i_image_2"],
            envelope=self._settings_envelope(task_id, "propose-sync-intent-gate"),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_config_saga_persisted":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.settings.confirm(
                task_id,
                "i_image_1",
                proposal["proposal_id"],
                envelope=self._settings_envelope(task_id, "confirm-sync-intent-gate"),
                crash_hook=crash,
            )

        target = self.settings.get(task_id, "i_image_2")
        self.assertFalse(target["editable"])
        self.assertTrue(target["pending_application"]["sync_target"])
        with self.assertRaises(HarnessError) as raised:
            self.settings.propose(
                task_id,
                "i_image_2",
                base_revision=target["revision"]["current"],
                patch={"candidate_concurrency": 2},
                sync_unstarted_image_work_items=False,
                expected_sync_instance_ids=[],
                envelope=self._settings_envelope(task_id, "propose-conflicting-target"),
            )
        self.assertEqual(raised.exception.code, "SETTINGS_REVISION_CONFLICT")

    def test_cross_task_sagas_do_not_block_settings_or_first_start(self) -> None:
        for saga_state in ("WAITING_SAFE_POINT", "FAILED"):
            with self.subTest(saga_state=saga_state):
                suffix = saga_state.lower()
                foreign_task_id = self._planned_task(f"task_foreign_saga_{suffix}")
                local_task_id = self._planned_task(f"task_local_saga_{suffix}")
                foreign_proposal_id = self._persist_runtime_saga(
                    foreign_task_id, saga_state
                )

                current = self.settings.get(local_task_id, "i_image_1")

                self.assertTrue(current["editable"])
                self.assertIsNone(current["pending_application"])
                proposal = self.settings.propose(
                    local_task_id,
                    "i_image_1",
                    base_revision=current["revision"]["current"],
                    patch={"watermark": True},
                    sync_unstarted_image_work_items=False,
                    expected_sync_instance_ids=[],
                    envelope=self._settings_envelope(
                        local_task_id, f"propose-local-{suffix}"
                    ),
                )
                self.assertEqual(proposal["status"], "DRAFT")

                self.settings.ensure_before_start(local_task_id, "i_image_1")
                foreign_sagas = self.settings._sagas_for_instance(
                    foreign_task_id, "i_image_1"
                )
                self.assertEqual(len(foreign_sagas), 1)
                self.assertEqual(foreign_sagas[0]["proposal_id"], foreign_proposal_id)
                self.assertEqual(foreign_sagas[0]["state"], saga_state)

    def test_pending_saga_uniqueness_is_scoped_to_task_and_instance(self) -> None:
        task_ids = (
            self._planned_task("task_pending_scope_a"),
            self._planned_task("task_pending_scope_b"),
        )
        proposal_ids = {
            task_id: self._persist_runtime_saga(task_id, "WAITING_SAFE_POINT")
            for task_id in task_ids
        }
        self.adapter.boundary = {
            "state": "candidate_generation",
            "checkpoint_id": None,
            "safe_now": False,
            "reason": "ACTIVE_JOB",
        }

        for task_id in task_ids:
            with self.subTest(task_id=task_id):
                current = self.settings.get(task_id, "i_image_1")
                self.assertFalse(current["editable"])
                self.assertEqual(
                    current["pending_application"]["proposal_id"],
                    proposal_ids[task_id],
                )
                pending = self.settings.apply_pending_if_safe(task_id, "i_image_1")
                self.assertEqual(pending["proposal_id"], proposal_ids[task_id])
                self.assertEqual(pending["status"], "WAITING_SAFE_POINT")

    def test_task_rebase_preserves_overrides_and_stops_at_start_lock(self) -> None:
        task_id = self._planned_task("task_rebase_settings")
        source = self.settings.get(task_id, "i_image_1")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=source["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-before-rebase"),
        )
        self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-before-rebase"),
        )
        changed = build_config_snapshot(revision="cfg_rebased")
        raw = changed.model_dump(mode="json")
        raw["runtime"]["image_agent"]["candidate_concurrency"] = 3
        self.task_config.process_snapshot = type(changed).model_validate(raw)

        rebased = self.rebase.rebase_all(
            actor={"actor_type": "system", "actor_id": "config_publisher"}
        )

        self.assertEqual(rebased["updated"], 1)
        current = self.materializer.revisions.read_current(task_id, "i_image_1")
        self.assertEqual(current["manifest"]["task_config_revision_id"], "task-config-r000002")
        self.assertTrue(current["manifest"]["overrides"]["watermark"])
        self.assertEqual(current["manifest"]["effective_runtime"]["candidate_concurrency"], 3)

        self.task_config.lock_for_start(task_id)
        self.task_config.process_snapshot = build_config_snapshot(revision="cfg_after_start")
        skipped = self.rebase.rebase_all(
            actor={"actor_type": "system", "actor_id": "config_publisher"}
        )
        self.assertEqual(skipped["skipped_started"], 1)
        self.assertEqual(
            self.task_config.get_current(task_id)["revision"]["revision_id"],
            "task-config-r000002",
        )

    def test_stale_instance_baseline_is_read_from_history_and_fails_closed(self) -> None:
        task_id = self._planned_task("task_stale_instance_baseline")
        before = self.settings.get(task_id, "i_image_1")
        inherited_before = before["values"]["candidate_concurrency"]["inherited"]
        changed = build_config_snapshot(revision="cfg_rebase_interrupted")
        raw = changed.model_dump(mode="json")
        raw["runtime"]["image_agent"]["candidate_concurrency"] = 2
        self.task_config.process_snapshot = type(changed).model_validate(raw)
        self.task_config.rebase(
            task_id,
            created_by={"type": "system", "id": "interrupted_rebase"},
        )

        stale = self.settings.get(task_id, "i_image_1")

        self.assertFalse(stale["editable"])
        self.assertEqual(
            stale["values"]["candidate_concurrency"]["inherited"],
            inherited_before,
        )
        with self.assertRaises(HarnessError) as raised:
            self.settings.propose(
                task_id,
                "i_image_1",
                base_revision=stale["revision"]["current"],
                patch={"watermark": True},
                sync_unstarted_image_work_items=False,
                expected_sync_instance_ids=[],
                envelope=self._settings_envelope(task_id, "propose-stale-baseline"),
            )
        self.assertEqual(raised.exception.code, "SETTINGS_REVISION_CONFLICT")

    def test_rebase_recovers_pointer_before_instance_projection(self) -> None:
        task_id = self._planned_task("task_rebase_projection_recovery")
        self.settings.get(task_id, "i_image_1")
        self.task_config.process_snapshot = build_config_snapshot(
            revision="cfg_rebase_projection_recovery"
        )
        with (
            patch.object(
                self.store,
                "update_instance_fields",
                side_effect=SimulatedCrash("after_instance_pointer"),
            ),
            self.assertRaises(SimulatedCrash),
        ):
            self.rebase.rebase_all(
                actor={"actor_type": "system", "actor_id": "config_publisher"}
            )

        recovered = self.rebase.rebase_all(
            actor={"actor_type": "system", "actor_id": "config_publisher"}
        )

        self.assertEqual(recovered["updated"], 1)
        instance = self.store.instance.get(task_id, "i_image_1")
        self.assertEqual(instance["config_revision"], 2)

    def test_malformed_safe_boundary_waits_without_remote_application(self) -> None:
        task_id = self._planned_task("task_malformed_safe_boundary")
        base = self.settings.get(task_id, "i_image_1")
        self._start_instance_projection(task_id, "i_image_1")
        self.adapter.boundary = {
            "state": "candidate_review",
            "checkpoint_id": None,
            "safe_now": True,
            "reason": None,
        }
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=base["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-malformed-boundary"),
        )

        result = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-malformed-boundary"),
        )

        self.assertEqual(result["status"], "WAITING_SAFE_POINT")
        self.assertEqual(self.adapter.apply_calls, 0)

    def test_safe_point_saga_recovers_after_remote_branch_creation(self) -> None:
        task_id = self._planned_task("task_safe_saga")
        base = self.settings.get(task_id, "i_image_1")
        self._start_instance_projection(task_id, "i_image_1")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=base["revision"]["current"],
            patch={"response_format": "b64_json"},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-safe"),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_config_child_branch_created":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.settings.confirm(
                task_id,
                "i_image_1",
                proposal["proposal_id"],
                envelope=self._settings_envelope(task_id, "confirm-safe"),
                crash_hook=crash,
            )

        recovered = self.settings.apply_pending_if_safe(task_id, "i_image_1")
        self.assertEqual(recovered["status"], "APPLIED_ON_BRANCH")
        self.assertEqual(recovered["branch_id"], "branch_config_1")
        self.assertEqual(len(self.adapter.receipts), 1)
        replay = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-safe"),
        )
        self.assertEqual(replay, recovered)

    def test_mismatched_remote_receipt_fails_without_advancing_pointer(self) -> None:
        task_id = self._planned_task("task_bad_safe_receipt")
        base = self.settings.get(task_id, "i_image_1")
        self._start_instance_projection(task_id, "i_image_1")
        self.adapter.receipt_from_checkpoint_override = "checkpoint_wrong"
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=base["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-bad-receipt"),
        )

        with self.assertRaises(HarnessError) as raised:
            self.settings.confirm(
                task_id,
                "i_image_1",
                proposal["proposal_id"],
                envelope=self._settings_envelope(task_id, "confirm-bad-receipt"),
            )

        self.assertEqual(raised.exception.code, "CONFIG_INTEGRITY_FAILED")
        current = self.materializer.revisions.read_current(task_id, "i_image_1")
        self.assertEqual(current["manifest"]["revision_id"], "cfg-inst-r000001")
        self.assertIsNone(current["state"]["pending_revision_id"])

        failed = self.settings.get(task_id, "i_image_1")
        self.assertTrue(failed["editable"])
        self.assertIsNone(failed["pending_application"])
        self.assertEqual(
            failed["last_application_failure"]["last_error"]["code"],
            "CONFIG_INTEGRITY_FAILED",
        )

        self.adapter.receipt_from_checkpoint_override = None
        retry = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=failed["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-after-bad-receipt"),
        )
        applied = self.settings.confirm(
            task_id,
            "i_image_1",
            retry["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-after-bad-receipt"),
        )
        self.assertEqual(applied["status"], "APPLIED_ON_BRANCH")
        self.assertEqual(applied["revision_id"], "cfg-inst-r000003")
        recovered = self.settings.get(task_id, "i_image_1")
        self.assertIsNone(recovered["last_application_failure"])

    def test_confirmation_recovers_an_intent_before_proposal_projection(self) -> None:
        task_id = self._planned_task("task_confirm_intent_recovery")
        base = self.settings.get(task_id, "i_image_1")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=base["revision"]["current"],
            patch={"candidate_concurrency": 3},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-intent-recovery"),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_config_saga_persisted":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.settings.confirm(
                task_id,
                "i_image_1",
                proposal["proposal_id"],
                envelope=self._settings_envelope(task_id, "confirm-intent-recovery"),
                crash_hook=crash,
            )

        recovered = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-intent-recovery"),
        )
        self.assertEqual(recovered["status"], "APPLIED_BEFORE_START")

    def test_startup_recovery_completes_a_persisted_confirmation_intent(self) -> None:
        task_id = self._planned_task("task_confirm_startup_recovery")
        base = self.settings.get(task_id, "i_image_1")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=base["revision"]["current"],
            patch={"candidate_concurrency": 3},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-startup-recovery"),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_config_saga_persisted":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.settings.confirm(
                task_id,
                "i_image_1",
                proposal["proposal_id"],
                envelope=self._settings_envelope(task_id, "confirm-startup-recovery"),
                crash_hook=crash,
            )

        recovered = self.settings.recover()

        self.assertEqual(len(recovered), 1)
        self.assertEqual(
            recovered[0]["result"]["status"], "APPLIED_BEFORE_START"
        )
        replay = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-startup-recovery"),
        )
        self.assertEqual(replay["status"], "APPLIED_BEFORE_START")

    def test_local_application_recovers_pointer_before_instance_projection(self) -> None:
        task_id = self._planned_task("task_local_projection_recovery")
        base = self.settings.get(task_id, "i_image_1")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=base["revision"]["current"],
            patch={"watermark": True},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, "propose-local-recovery"),
        )

        def crash(checkpoint: str) -> None:
            if checkpoint == "after_local_pointer_committed:i_image_1":
                raise SimulatedCrash(checkpoint)

        with self.assertRaises(SimulatedCrash):
            self.settings.confirm(
                task_id,
                "i_image_1",
                proposal["proposal_id"],
                envelope=self._settings_envelope(task_id, "confirm-local-recovery"),
                crash_hook=crash,
            )

        instance = self.store.instance.get(task_id, "i_image_1")
        self.assertEqual(instance["config_revision"], 1)
        recovered = self.settings.confirm(
            task_id,
            "i_image_1",
            proposal["proposal_id"],
            envelope=self._settings_envelope(task_id, "confirm-local-recovery"),
        )
        self.assertEqual(recovered["status"], "APPLIED_BEFORE_START")
        instance = self.store.instance.get(task_id, "i_image_1")
        self.assertEqual(instance["config_revision"], 2)

    def _planned_task(self, task_id: str, *, count: int = 1) -> str:
        created = create_task(self.commands, task_id)
        self.task_config.pin(task_id)
        draft = image_plan(task_id, count=count)
        self.commands.save_plan(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope(f"save-{task_id}", created["revision"]),
        )
        return task_id

    def _start_instance_projection(self, task_id: str, instance_id: str) -> None:
        current = self.store.task.revision(task_id, task_id)
        self.commands.confirm_start(task_id, envelope(f"confirm-{task_id}", current))
        for status in ("STARTING", "RUNNING"):
            current = self.store.task.revision(task_id, task_id)
            self.commands.transition_instance(
                task_id,
                instance_id,
                status,
                envelope(f"{status.lower()}-{task_id}", current, actor_type="adapter"),
            )

    def _persist_runtime_saga(self, task_id: str, saga_state: str) -> str:
        base = self.settings.get(task_id, "i_image_1")
        self._start_instance_projection(task_id, "i_image_1")
        original_boundary = deepcopy(self.adapter.boundary)
        original_receipt_override = self.adapter.receipt_from_checkpoint_override
        if saga_state == "WAITING_SAFE_POINT":
            self.adapter.boundary = {
                "state": "candidate_generation",
                "checkpoint_id": None,
                "safe_now": False,
                "reason": "ACTIVE_JOB",
            }
        elif saga_state == "FAILED":
            self.adapter.receipt_from_checkpoint_override = "checkpoint_wrong_task_scope"
        else:
            raise AssertionError(f"unsupported saga state: {saga_state}")
        proposal = self.settings.propose(
            task_id,
            "i_image_1",
            base_revision=base["revision"]["current"],
            patch={"candidate_concurrency": 2},
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=self._settings_envelope(task_id, f"propose-{saga_state.lower()}"),
        )
        try:
            if saga_state == "FAILED":
                with self.assertRaises(HarnessError) as raised:
                    self.settings.confirm(
                        task_id,
                        "i_image_1",
                        proposal["proposal_id"],
                        envelope=self._settings_envelope(
                            task_id, f"confirm-{saga_state.lower()}"
                        ),
                    )
                self.assertEqual(raised.exception.code, "CONFIG_INTEGRITY_FAILED")
            else:
                result = self.settings.confirm(
                    task_id,
                    "i_image_1",
                    proposal["proposal_id"],
                    envelope=self._settings_envelope(
                        task_id, f"confirm-{saga_state.lower()}"
                    ),
                )
                self.assertEqual(result["status"], "WAITING_SAFE_POINT")
        finally:
            self.adapter.boundary = original_boundary
            self.adapter.receipt_from_checkpoint_override = original_receipt_override
        return proposal["proposal_id"]

    def _settings_envelope(self, task_id: str, key: str) -> CommandEnvelope:
        return envelope(key, self.store.task.revision(task_id, task_id))


class RuntimeSettingsApiTests(unittest.TestCase):
    def test_get_propose_confirm_and_metrics_routes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(
                HarnessSettings(
                    control_root=root / "control-data",
                    workspace_root=root / "workspace",
                    contracts_root=ROOT / "contracts" / "v1",
                    config_snapshot=build_config_snapshot(),
                )
            )
            with TestClient(app) as client:
                container = app.state.container
                created = create_task(container.commands, "task_runtime_api")
                container.task_config.pin("task_runtime_api")
                draft = image_plan("task_runtime_api")
                container.commands.save_plan(
                    "task_runtime_api",
                    stages=draft["stages"],
                    instances=draft["instances"],
                    task_cards=draft["task_cards"],
                    envelope=envelope("save-runtime-api", created["revision"]),
                )
                current = client.get("/api/v1/instances/i_image_1/runtime-settings")
                self.assertEqual(current.status_code, 200, current.text)
                revision = current.json()["revision"]["current"]
                task_revision = container.store.task.revision(
                    "task_runtime_api", "task_runtime_api"
                )
                proposed = client.post(
                    "/api/v1/instances/i_image_1/runtime-setting-proposals",
                    json={
                        "base_revision": revision,
                        "overrides": {"watermark": True},
                        "sync_unstarted_image_work_items": False,
                        "expected_sync_instance_ids": [],
                        "envelope": self._body_envelope("runtime-api-propose", task_revision),
                    },
                )
                self.assertEqual(proposed.status_code, 200, proposed.text)
                proposal_id = proposed.json()["proposal_id"]
                confirmed = client.post(
                    (
                        "/api/v1/instances/i_image_1/runtime-setting-proposals/"
                        f"{proposal_id}/confirm"
                    ),
                    json={
                        "envelope": self._body_envelope(
                            "runtime-api-confirm", task_revision
                        )
                    },
                )
                self.assertEqual(confirmed.status_code, 200, confirmed.text)
                self.assertEqual(confirmed.json()["status"], "APPLIED_BEFORE_START")
                metrics = client.get("/api/v1/runtime-settings/metrics")
                self.assertEqual(metrics.status_code, 200, metrics.text)
                self.assertEqual(metrics.json()["counters"]["proposals_confirmed"], 1)

    @staticmethod
    def _body_envelope(key: str, expected_revision: int) -> dict:
        return {
            "idempotency_key": key,
            "actor_type": "human",
            "actor_id": "tester",
            "expected_revision": expected_revision,
        }


if __name__ == "__main__":
    unittest.main()
