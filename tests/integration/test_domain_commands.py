from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.errors import HarnessError
from runtime_helpers import (
    build_service,
    card,
    create_task,
    envelope,
    image_plan,
    image_to_ppt_plan,
    instance,
    ppt_plan,
    stage,
)


class TaskCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.service = build_service(self.root)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _save(
        self,
        task_id: str,
        draft: dict[str, list[dict]],
        expected: int = 1,
        key: str = "save-plan",
    ) -> dict:
        return self.service.save_plan(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope(key, expected),
        )

    def _transition(self, task_id: str, instance_id: str, status: str, key: str) -> dict:
        revision = self.store.task.revision(task_id, task_id)
        return self.service.transition_instance(
            task_id,
            instance_id,
            status,
            envelope(envelope_key(task_id, key), revision, "adapter"),
        )

    def test_manual_plan_and_idempotency(self) -> None:
        create_task(self.service, "t_manual")
        draft = image_plan("t_manual")
        saved = self._save("t_manual", draft)
        self.assertEqual(saved["task"]["status"], "AWAITING_START_CONFIRMATION")
        self.assertEqual(saved["task_revision"], 2)
        self.assertEqual(saved["plan"]["stages"][0]["status"], "READY")
        replay = self._save("t_manual", draft)
        self.assertEqual(replay, saved)
        changed = image_plan("t_manual", count=2)
        with self.assertRaises(HarnessError) as captured:
            self._save("t_manual", changed)
        self.assertEqual(captured.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_manual_confirmation_starts_ready_image_work(self) -> None:
        create_task(self.service, "t_confirm")
        saved = self._save("t_confirm", image_plan("t_confirm"))
        confirmed = self.service.confirm_start(
            "t_confirm", envelope("confirm", saved["task_revision"])
        )
        self.assertEqual(confirmed["task"]["status"], "RUNNING")
        lifecycle = confirmed["plan"]["instances"][0]["requirement_lifecycle"]
        self.assertIsNotNone(lifecycle["first_activated_at"])

    def test_input_registration_is_revisioned_and_image_only_can_complete(self) -> None:
        created = create_task(self.service, "t_complete", "auto")
        registered = self.service.register_input_manifest(
            "t_complete",
            "inputs/manifests/selected.json",
            envelope("register-input", created["revision"]),
        )
        self.assertEqual(registered["task"]["input_manifest"], "inputs/manifests/selected.json")
        self._save(
            "t_complete",
            image_plan("t_complete"),
            expected=registered["revision"],
            key="save-complete",
        )
        self._transition("t_complete", "i_image_1", "STARTING", "starting")
        self._transition("t_complete", "i_image_1", "RUNNING", "running")
        result = self._transition("t_complete", "i_image_1", "SUCCEEDED", "done")
        self.assertEqual(result["task"]["status"], "SUCCEEDED")

    def test_ppt_only_blocks_only_when_activated(self) -> None:
        create_task(self.service, "t_ppt_manual")
        saved = self._save("t_ppt_manual", ppt_plan("t_ppt_manual"))
        self.assertEqual(saved["task"]["status"], "AWAITING_START_CONFIRMATION")
        self.assertIsNone(
            saved["plan"]["instances"][0]["requirement_lifecycle"]["first_activated_at"]
        )
        confirmed = self.service.confirm_start(
            "t_ppt_manual", envelope("confirm-ppt", saved["task_revision"])
        )
        self.assertEqual(confirmed["task"]["status"], "BLOCKED_UNAVAILABLE")
        self.assertIsNotNone(
            confirmed["plan"]["instances"][0]["requirement_lifecycle"]["first_activated_at"]
        )

    def test_manual_optional_ppt_only_is_explicitly_skipped_without_partial(self) -> None:
        create_task(self.service, "t_ppt_optional")
        saved = self._save(
            "t_ppt_optional",
            ppt_plan("t_ppt_optional", required=False),
        )
        self.assertEqual(saved["task"]["status"], "AWAITING_START_CONFIRMATION")
        self.assertEqual(saved["plan"]["stages"][0]["status"], "SKIPPED")

        confirmed = self.service.confirm_start(
            "t_ppt_optional",
            envelope("confirm-ppt-optional", saved["task_revision"]),
        )

        self.assertEqual(confirmed["task"]["status"], "SUCCEEDED")
        self.assertEqual(confirmed["plan"]["stages"][0]["status"], "SKIPPED")
        self.assertNotEqual(confirmed["task"]["status"], "PARTIAL")

    def test_required_ppt_is_delayed_until_image_succeeds_and_survives_restart(self) -> None:
        create_task(self.service, "t_delayed", "auto")
        saved = self._save("t_delayed", image_to_ppt_plan("t_delayed"))
        self.assertEqual(saved["task"]["status"], "RUNNING")
        self.assertEqual(saved["plan"]["stages"][1]["status"], "PENDING")
        self.assertIsNone(
            saved["plan"]["instances"][1]["requirement_lifecycle"]["first_activated_at"]
        )
        self._transition("t_delayed", "i_image_1", "STARTING", "starting")
        self._transition("t_delayed", "i_image_1", "RUNNING", "running")
        finished = self._transition("t_delayed", "i_image_1", "SUCCEEDED", "succeeded")
        self.assertEqual(finished["task"]["status"], "BLOCKED_UNAVAILABLE")
        self.assertEqual(finished["plan"]["stages"][1]["status"], "UNAVAILABLE")
        self.assertIsNotNone(
            finished["plan"]["instances"][1]["requirement_lifecycle"]["first_activated_at"]
        )
        self.store.close()
        recovered_store, recovered_service = build_service(self.root)
        self.store = recovered_store
        self.service = recovered_service
        recovered = self.store.plan.get("t_delayed", "t_delayed")
        self.assertEqual(recovered["task"]["status"], "BLOCKED_UNAVAILABLE")

    def test_initial_optional_ppt_is_skipped_without_partial_result(self) -> None:
        create_task(self.service, "t_optional", "auto")
        self._save("t_optional", image_to_ppt_plan("t_optional", ppt_required=False))
        self._transition("t_optional", "i_image_1", "STARTING", "starting")
        self._transition("t_optional", "i_image_1", "RUNNING", "running")
        finished = self._transition("t_optional", "i_image_1", "SUCCEEDED", "succeeded")
        self.assertEqual(finished["plan"]["stages"][1]["status"], "SKIPPED")
        self.assertEqual(finished["task"]["status"], "SUCCEEDED")

    def test_running_work_has_priority_over_waiting_approval(self) -> None:
        create_task(self.service, "t_priority", "auto")
        self._save("t_priority", image_plan("t_priority", 2))
        self._transition("t_priority", "i_image_1", "STARTING", "one-start")
        self._transition("t_priority", "i_image_1", "RUNNING", "one-run")
        waiting = self._transition("t_priority", "i_image_1", "WAITING_APPROVAL", "one-wait")
        self.assertEqual(waiting["task"]["status"], "RUNNING")
        self._transition("t_priority", "i_image_2", "STARTING", "two-start")
        self._transition("t_priority", "i_image_2", "RUNNING", "two-run")
        finished_two = self._transition("t_priority", "i_image_2", "SUCCEEDED", "two-done")
        self.assertEqual(finished_two["task"]["status"], "WAITING_APPROVAL")

    def test_activated_authorized_downgrade_produces_partial(self) -> None:
        create_task(self.service, "t_partial", "auto")
        self._save("t_partial", image_plan("t_partial", 2))
        self._transition("t_partial", "i_image_1", "STARTING", "one-start")
        self._transition("t_partial", "i_image_1", "RUNNING", "one-run")
        self._transition("t_partial", "i_image_1", "SUCCEEDED", "one-done")
        self._transition("t_partial", "i_image_2", "STARTING", "two-start")
        self._transition("t_partial", "i_image_2", "RUNNING", "two-run")
        self._transition("t_partial", "i_image_2", "WAITING_APPROVAL", "two-wait")
        revision = self.store.task.revision("t_partial", "t_partial")
        result = self.service.downgrade_instance(
            "t_partial",
            "i_image_2",
            "The approved first visual is sufficient for this delivery.",
            envelope("downgrade-two", revision),
        )
        self.assertEqual(result["task"]["status"], "PARTIAL")
        downgraded = next(
            item for item in result["plan"]["instances"] if item["instance_id"] == "i_image_2"
        )
        self.assertFalse(downgraded["required"])
        self.assertIsNotNone(downgraded["requirement_lifecycle"]["authorized_downgrade"])

    def test_unsupported_topology_and_illegal_transition_are_rejected(self) -> None:
        create_task(self.service, "t_invalid", "auto")
        invalid = {
            "stages": [
                stage("t_invalid", "s_ppt", "ppt", 1, [], True, ["i_ppt"]),
                stage("t_invalid", "s_image", "image", 2, ["s_ppt"], True, ["i_image"]),
            ],
            "instances": [
                instance("t_invalid", "i_ppt", "s_ppt", "ppt", True),
                instance("t_invalid", "i_image", "s_image", "image", True),
            ],
            "task_cards": [
                card("t_invalid", "i_ppt", "s_ppt", "ppt"),
                card("t_invalid", "i_image", "s_image", "image"),
            ],
        }
        with self.assertRaises(HarnessError) as captured:
            self._save("t_invalid", invalid)
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")

        create_task(self.service, "t_illegal", "auto")
        self._save("t_illegal", image_plan("t_illegal"))
        with self.assertRaises(HarnessError) as transition:
            self._transition("t_illegal", "i_image_1", "SUCCEEDED", "skip-runtime")
        self.assertEqual(transition.exception.code, "INVALID_STATE_TRANSITION")

        create_task(self.service, "t_failed_restart", "auto")
        self._save("t_failed_restart", image_plan("t_failed_restart"))
        self._transition("t_failed_restart", "i_image_1", "STARTING", "start")
        self._transition("t_failed_restart", "i_image_1", "RUNNING", "run")
        self._transition("t_failed_restart", "i_image_1", "FAILED", "fail")
        with self.assertRaises(HarnessError) as direct_resume:
            self._transition("t_failed_restart", "i_image_1", "RUNNING", "bypass-restart")
        self.assertEqual(direct_resume.exception.code, "INVALID_STATE_TRANSITION")

    def test_same_type_parallel_stages_are_accepted(self) -> None:
        create_task(self.service, "t_parallel", "auto")
        parallel = {
            "stages": [
                stage("t_parallel", "s_image_1", "image", 1, [], True, ["i_image_1"]),
                stage("t_parallel", "s_image_2", "image", 2, [], True, ["i_image_2"]),
            ],
            "instances": [
                instance("t_parallel", "i_image_1", "s_image_1", "image", True),
                instance("t_parallel", "i_image_2", "s_image_2", "image", True),
            ],
            "task_cards": [
                card("t_parallel", "i_image_1", "s_image_1", "image"),
                card("t_parallel", "i_image_2", "s_image_2", "image"),
            ],
        }
        saved = self._save("t_parallel", parallel)
        self.assertEqual(len(saved["plan"]["stages"]), 2)
        self.assertEqual(len(saved["plan"]["instances"]), 2)
        self.assertEqual(saved["plan"]["stages"][0]["status"], "READY")
        self.assertEqual(saved["plan"]["stages"][1]["status"], "READY")

    def test_parallel_images_can_feed_a_ppt_stage(self) -> None:
        create_task(self.service, "t_fan_in", "auto")
        fan_in = {
            "stages": [
                stage("t_fan_in", "s_image_1", "image", 1, [], True, ["i_image_1"]),
                stage("t_fan_in", "s_image_2", "image", 2, [], True, ["i_image_2"]),
                stage(
                    "t_fan_in",
                    "s_ppt",
                    "ppt",
                    3,
                    ["s_image_1", "s_image_2"],
                    True,
                    ["i_ppt_1"],
                ),
            ],
            "instances": [
                instance("t_fan_in", "i_image_1", "s_image_1", "image", True),
                instance("t_fan_in", "i_image_2", "s_image_2", "image", True),
                instance("t_fan_in", "i_ppt_1", "s_ppt", "ppt", True),
            ],
            "task_cards": [
                card("t_fan_in", "i_image_1", "s_image_1", "image"),
                card("t_fan_in", "i_image_2", "s_image_2", "image"),
                card("t_fan_in", "i_ppt_1", "s_ppt", "ppt"),
            ],
        }
        saved = self._save("t_fan_in", fan_in)
        self.assertEqual(len(saved["plan"]["stages"]), 3)

    def test_same_type_stage_dependency_is_rejected(self) -> None:
        create_task(self.service, "t_chained", "auto")
        chained = {
            "stages": [
                stage("t_chained", "s_image_1", "image", 1, [], True, ["i_image_1"]),
                stage(
                    "t_chained", "s_image_2", "image", 2, ["s_image_1"], True, ["i_image_2"]
                ),
            ],
            "instances": [
                instance("t_chained", "i_image_1", "s_image_1", "image", True),
                instance("t_chained", "i_image_2", "s_image_2", "image", True),
            ],
            "task_cards": [
                card("t_chained", "i_image_1", "s_image_1", "image"),
                card("t_chained", "i_image_2", "s_image_2", "image"),
            ],
        }
        with self.assertRaises(HarnessError) as captured:
            self._save("t_chained", chained)
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")

    def test_cancel_is_a_domain_command_and_preserves_workspace(self) -> None:
        created = create_task(self.service, "t_cancel")
        cancelled = self.service.cancel_task(
            "t_cancel", envelope("cancel-draft", created["revision"])
        )
        self.assertEqual(cancelled["task"]["status"], "CANCELLED")
        workspace = self.store.layout.workspace_root / "tasks" / "t_cancel"
        self.assertTrue(workspace.is_dir())

    def test_command_scope_rejects_path_like_task_ids_before_locking(self) -> None:
        with self.assertRaises(HarnessError) as captured:
            create_task(self.service, "../../../../escaped")

        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")
        self.assertFalse((self.store.layout.control_root.parent / "escaped.lock").exists())


def envelope_key(task_id: str, suffix: str) -> str:
    return f"{task_id}-{suffix}"
