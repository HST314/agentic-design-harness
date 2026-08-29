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

    def test_manual_finished_is_reversible_image_only_state(self) -> None:
        create_task(self.service, "t_manual_finished")
        saved = self._save(
            "t_manual_finished", image_to_ppt_plan("t_manual_finished")
        )
        image = next(
            item
            for item in saved["plan"]["instances"]
            if item["agent_type"] == "image"
        )
        self.assertFalse(image["manual_finished"])

        finished = self.service.set_manual_finished(
            "t_manual_finished",
            "i_image_1",
            True,
            envelope("finish-image", saved["task_revision"]),
        )
        self.assertTrue(
            next(
                item
                for item in finished["plan"]["instances"]
                if item["instance_id"] == "i_image_1"
            )["manual_finished"]
        )
        resumed = self.service.set_manual_finished(
            "t_manual_finished",
            "i_image_1",
            False,
            envelope("resume-image", finished["task_revision"]),
        )
        self.assertFalse(
            next(
                item
                for item in resumed["plan"]["instances"]
                if item["instance_id"] == "i_image_1"
            )["manual_finished"]
        )

        with self.assertRaises(HarnessError) as captured:
            self.service.set_manual_finished(
                "t_manual_finished",
                "i_ppt_1",
                True,
                envelope("finish-ppt", resumed["task_revision"]),
            )
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")

        with self.assertRaises(HarnessError) as captured:
            self.service.set_manual_finished(
                "t_manual_finished",
                "i_image_1",
                True,
                envelope(
                    "master-finish-image",
                    resumed["task_revision"],
                    actor_type="master",
                ),
            )
        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")

    def test_manual_business_status_is_cleared_by_the_next_runtime_transition(self) -> None:
        create_task(self.service, "t_manual_business_status")
        saved = self._save(
            "t_manual_business_status", image_plan("t_manual_business_status")
        )

        updated = saved
        for index, status in enumerate(
            ("TODO", "RUNNING", "WAITING_APPROVAL", "COMPLETED"), start=1
        ):
            updated = self.service.set_manual_business_status(
                "t_manual_business_status",
                "i_image_1",
                status,
                envelope(f"set-image-card-status-{index}", updated["task_revision"]),
            )
            instance = updated["plan"]["instances"][0]
            self.assertEqual(instance["manual_business_status"], status)
            self.assertEqual(instance["manual_finished"], status == "COMPLETED")

        started = self.service.transition_instance(
            "t_manual_business_status",
            "i_image_1",
            "STARTING",
            envelope(
                "runtime-starts-image",
                updated["task_revision"],
                actor_type="system",
            ),
        )
        instance = started["plan"]["instances"][0]
        self.assertNotIn("manual_business_status", instance)
        self.assertFalse(instance["manual_finished"])

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

    def test_ppt_only_becomes_running_when_activated(self) -> None:
        create_task(self.service, "t_ppt_manual")
        saved = self._save("t_ppt_manual", ppt_plan("t_ppt_manual"))
        self.assertEqual(saved["task"]["status"], "AWAITING_START_CONFIRMATION")
        self.assertIsNone(
            saved["plan"]["instances"][0]["requirement_lifecycle"]["first_activated_at"]
        )
        confirmed = self.service.confirm_start(
            "t_ppt_manual", envelope("confirm-ppt", saved["task_revision"])
        )
        self.assertEqual(confirmed["task"]["status"], "RUNNING")
        self.assertEqual(confirmed["plan"]["instances"][0]["status"], "READY")
        self.assertIsNotNone(
            confirmed["plan"]["instances"][0]["requirement_lifecycle"]["first_activated_at"]
        )

    def test_manual_optional_ppt_is_available_after_confirmation(self) -> None:
        create_task(self.service, "t_ppt_optional")
        saved = self._save(
            "t_ppt_optional",
            ppt_plan("t_ppt_optional", required=False),
        )
        self.assertEqual(saved["task"]["status"], "AWAITING_START_CONFIRMATION")
        self.assertEqual(saved["plan"]["stages"][0]["status"], "READY")

        confirmed = self.service.confirm_start(
            "t_ppt_optional",
            envelope("confirm-ppt-optional", saved["task_revision"]),
        )

        self.assertEqual(confirmed["task"]["status"], "RUNNING")
        self.assertEqual(confirmed["plan"]["stages"][0]["status"], "READY")

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
        self.assertEqual(finished["task"]["status"], "RUNNING")
        self.assertEqual(finished["plan"]["stages"][1]["status"], "READY")
        self.assertIsNotNone(
            finished["plan"]["instances"][1]["requirement_lifecycle"]["first_activated_at"]
        )
        self.store.close()
        recovered_store, recovered_service = build_service(self.root)
        self.store = recovered_store
        self.service = recovered_service
        recovered = self.store.plan.get("t_delayed", "t_delayed")
        self.assertEqual(recovered["task"]["status"], "RUNNING")

    def test_initial_optional_ppt_activates_after_image_succeeds(self) -> None:
        create_task(self.service, "t_optional", "auto")
        self._save("t_optional", image_to_ppt_plan("t_optional", ppt_required=False))
        self._transition("t_optional", "i_image_1", "STARTING", "starting")
        self._transition("t_optional", "i_image_1", "RUNNING", "running")
        finished = self._transition("t_optional", "i_image_1", "SUCCEEDED", "succeeded")
        self.assertEqual(finished["plan"]["stages"][1]["status"], "READY")
        self.assertEqual(finished["task"]["status"], "RUNNING")

    def test_waiting_approval_has_priority_over_running_work(self) -> None:
        create_task(self.service, "t_priority", "auto")
        self._save("t_priority", image_plan("t_priority", 2))
        self._transition("t_priority", "i_image_1", "STARTING", "one-start")
        self._transition("t_priority", "i_image_1", "RUNNING", "one-run")
        waiting = self._transition("t_priority", "i_image_1", "WAITING_APPROVAL", "one-wait")
        self.assertEqual(waiting["task"]["status"], "WAITING_APPROVAL")
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


class PlanAppendGateTests(unittest.TestCase):
    """Save-plan mode gates for replacing, extending, and revising live plans."""

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
        mode: str = "replace",
        plan_revision: int | None = None,
    ) -> dict:
        return self.service.save_plan(
            task_id,
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope(key, expected),
            mode=mode,
            expected_plan_revision=plan_revision,
        )

    def _transition(self, task_id: str, instance_id: str, status: str, key: str) -> dict:
        revision = self.store.task.revision(task_id, task_id)
        return self.service.transition_instance(
            task_id,
            instance_id,
            status,
            envelope(envelope_key(task_id, key), revision, "adapter"),
        )

    def _running_task(self, task_id: str) -> None:
        create_task(self.service, task_id, "auto")
        self._save(task_id, image_plan(task_id))
        self._transition(task_id, "i_image_1", "STARTING", "starting")
        self._transition(task_id, "i_image_1", "RUNNING", "running")

    @staticmethod
    def _append_draft(task_id: str) -> dict[str, list[dict]]:
        return {
            "stages": [stage(task_id, "s_image_2", "image", 2, [], True, ["i_image_2"])],
            "instances": [instance(task_id, "i_image_2", "s_image_2", "image", True)],
            "task_cards": [card(task_id, "i_image_2", "s_image_2", "image")],
        }

    def test_running_task_accepts_plan_append(self) -> None:
        self._running_task("t_append_running")
        revision = self.store.task.revision("t_append_running", "t_append_running")

        result = self._save(
            "t_append_running",
            self._append_draft("t_append_running"),
            expected=revision,
            key="append-running",
            mode="append",
        )

        plan = result["plan"]
        self.assertEqual(result["task"]["status"], "RUNNING")
        self.assertEqual(result["task"]["plan_revision"], 2)
        self.assertEqual(len(plan["stages"]), 2)
        self.assertEqual(len(plan["instances"]), 2)
        self.assertEqual(len(plan["task_cards"]), 2)
        landed_stage = plan["stages"][0]
        self.assertEqual(landed_stage["stage_id"], "s_image")
        self.assertEqual(landed_stage["status"], "RUNNING")
        landed_instance = plan["instances"][0]
        self.assertEqual(landed_instance["instance_id"], "i_image_1")
        self.assertEqual(landed_instance["status"], "RUNNING")
        appended_stage = plan["stages"][1]
        self.assertEqual(appended_stage["stage_id"], "s_image_2")
        self.assertEqual(appended_stage["status"], "READY")
        self.assertEqual(appended_stage["depends_on"], [])
        appended_instance = plan["instances"][1]
        self.assertEqual(appended_instance["status"], "READY")
        self.assertIsNotNone(
            appended_instance["requirement_lifecycle"]["first_activated_at"]
        )

    def test_running_task_still_rejects_plan_replace(self) -> None:
        self._running_task("t_replace_running")
        revision = self.store.task.revision("t_replace_running", "t_replace_running")

        with self.assertRaises(HarnessError) as captured:
            self._save(
                "t_replace_running",
                image_plan("t_replace_running", 2),
                expected=revision,
                key="replace-running",
                mode="replace",
            )

        self.assertEqual(captured.exception.code, "INVALID_STATE_TRANSITION")
        self.assertEqual(
            captured.exception.message,
            "A plan cannot be replaced while this task state is active or terminal.",
        )

    def test_append_cannot_reference_a_landed_running_stage(self) -> None:
        self._running_task("t_append_collision")
        revision = self.store.task.revision("t_append_collision", "t_append_collision")
        payload = {
            "stages": [
                stage("t_append_collision", "s_image", "image", 2, [], True, ["i_image_2"])
            ],
            "instances": [instance("t_append_collision", "i_image_2", "s_image", "image", True)],
            "task_cards": [card("t_append_collision", "i_image_2", "s_image", "image")],
        }

        with self.assertRaises(HarnessError) as captured:
            self._save(
                "t_append_collision",
                payload,
                expected=revision,
                key="append-collision",
                mode="append",
            )

        self.assertEqual(captured.exception.code, "VALIDATION_ERROR")

    def test_waiting_approval_task_accepts_plan_append(self) -> None:
        create_task(self.service, "t_append_waiting", "auto")
        self._save("t_append_waiting", image_plan("t_append_waiting"))
        self._transition("t_append_waiting", "i_image_1", "STARTING", "starting")
        self._transition("t_append_waiting", "i_image_1", "RUNNING", "running")
        waiting = self._transition("t_append_waiting", "i_image_1", "WAITING_APPROVAL", "waiting")
        self.assertEqual(waiting["task"]["status"], "WAITING_APPROVAL")
        revision = self.store.task.revision("t_append_waiting", "t_append_waiting")

        result = self._save(
            "t_append_waiting",
            self._append_draft("t_append_waiting"),
            expected=revision,
            key="append-waiting",
            mode="append",
        )

        self.assertEqual(result["task"]["status"], "WAITING_APPROVAL")
        self.assertEqual(result["plan"]["stages"][1]["status"], "READY")

    def test_append_honors_expected_plan_revision(self) -> None:
        self._running_task("t_append_ifmatch")
        revision = self.store.task.revision("t_append_ifmatch", "t_append_ifmatch")

        with self.assertRaises(HarnessError) as captured:
            self._save(
                "t_append_ifmatch",
                self._append_draft("t_append_ifmatch"),
                expected=revision,
                key="append-ifmatch-stale",
                mode="append",
                plan_revision=99,
            )
        self.assertEqual(captured.exception.code, "REVISION_CONFLICT")

        result = self._save(
            "t_append_ifmatch",
            self._append_draft("t_append_ifmatch"),
            expected=revision,
            key="append-ifmatch",
            mode="append",
            plan_revision=1,
        )
        self.assertEqual(result["task"]["plan_revision"], 2)

    def test_append_can_chain_ppt_behind_a_running_image_stage(self) -> None:
        self._running_task("t_append_ppt")
        revision = self.store.task.revision("t_append_ppt", "t_append_ppt")
        payload = {
            "stages": [stage("t_append_ppt", "s_ppt", "ppt", 2, ["s_image"], True, ["i_ppt_1"])],
            "instances": [instance("t_append_ppt", "i_ppt_1", "s_ppt", "ppt", True)],
            "task_cards": [card("t_append_ppt", "i_ppt_1", "s_ppt", "ppt")],
        }

        result = self._save(
            "t_append_ppt",
            payload,
            expected=revision,
            key="append-ppt",
            mode="append",
        )

        plan = result["plan"]
        self.assertEqual(result["task"]["status"], "RUNNING")
        appended_stage = plan["stages"][1]
        self.assertEqual(appended_stage["type"], "ppt")
        self.assertEqual(appended_stage["depends_on"], ["s_image"])
        self.assertEqual(appended_stage["status"], "PENDING")

    def test_merge_updates_only_unstarted_cards_and_preserves_started_instances(self) -> None:
        task_id = "t_merge_cards"
        create_task(self.service, task_id, "auto")
        self._save(task_id, image_plan(task_id, count=3))
        self._transition(task_id, "i_image_1", "STARTING", "starting")
        self._transition(task_id, "i_image_1", "RUNNING", "running")

        landed = self.store.plan.get(task_id, task_id)
        self.assertIsNotNone(landed)
        assert landed is not None
        started_instance = next(
            item for item in landed["instances"] if item["instance_id"] == "i_image_1"
        )
        started_card = next(
            item for item in landed["task_cards"] if item["instance_id"] == "i_image_1"
        )
        unchanged_card = next(
            item for item in landed["task_cards"] if item["instance_id"] == "i_image_3"
        )
        started_projection_revision = self.store.instance.revision(task_id, "i_image_1")

        revised = image_plan(task_id, count=3)
        revised_started_card = next(
            item for item in revised["task_cards"] if item["instance_id"] == "i_image_1"
        )
        revised_started_card.update(
            {"revision": 9, "objective": "This started card must not be replaced."}
        )
        revised_unstarted_card = next(
            item for item in revised["task_cards"] if item["instance_id"] == "i_image_2"
        )
        revised_unstarted_card.update(
            {"revision": 2, "objective": "Use the revised direction for the unstarted card."}
        )

        merged = self._save(
            task_id,
            revised,
            expected=self.store.task.revision(task_id, task_id),
            key="merge-unstarted-card",
            mode="merge",
            plan_revision=landed["task"]["plan_revision"],
        )

        merged_started_instance = next(
            item
            for item in merged["plan"]["instances"]
            if item["instance_id"] == "i_image_1"
        )
        merged_started_card = next(
            item
            for item in merged["plan"]["task_cards"]
            if item["instance_id"] == "i_image_1"
        )
        merged_unstarted_card = next(
            item
            for item in merged["plan"]["task_cards"]
            if item["instance_id"] == "i_image_2"
        )
        merged_unchanged_card = next(
            item
            for item in merged["plan"]["task_cards"]
            if item["instance_id"] == "i_image_3"
        )
        self.assertEqual(merged_started_instance, started_instance)
        self.assertEqual(merged_started_card, started_card)
        self.assertEqual(merged_unstarted_card["revision"], 2)
        self.assertEqual(
            merged_unstarted_card["objective"],
            "Use the revised direction for the unstarted card.",
        )
        self.assertEqual(merged_unchanged_card, unchanged_card)
        self.assertEqual(
            self.store.instance.revision(task_id, "i_image_1"),
            started_projection_revision,
        )


def envelope_key(task_id: str, suffix: str) -> str:
    return f"{task_id}-{suffix}"
