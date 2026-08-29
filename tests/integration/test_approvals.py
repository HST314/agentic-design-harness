from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.errors import HarnessError
from harness.services.approvals import ApprovalInboxService
from harness.storage.repository import Actor
from runtime_helpers import build_service, build_store, create_task, envelope, image_plan


class ApprovalInboxServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        create_task(self.commands, "t_approvals")
        draft = image_plan("t_approvals")
        self.commands.save_plan(
            "t_approvals",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope("save-approval-plan", 1),
        )
        self.approvals = ApprovalInboxService(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_owner_is_frozen_and_polling_is_deduped_across_restart(self) -> None:
        first = self.approvals.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_human_approval",
            capabilities=["approve_taskbook"],
            context={"phase": "confirmation_build"},
            operation_id="job_first",
        )
        revision = self.store.task.revision("t_approvals", "t_approvals")
        self.commands.set_approval_mode(
            "t_approvals",
            "i_image_1",
            "master",
            envelope("route-future-approvals", revision),
        )

        replay = self.approvals.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_human_approval",
            capabilities=["approve_taskbook"],
            context={"phase": "confirmation_build"},
            operation_id="job_first",
        )
        second = self.approvals.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_master_selection",
            capabilities=["select_master"],
            context={"candidates": [{"candidate_id": "candidate_one"}]},
            operation_id="job_second",
        )

        self.assertEqual(replay["approval"], first["approval"])
        self.assertEqual(first["approval"]["owner"], "human")
        self.assertEqual(second["approval"]["owner"], "master")
        self.assertLess(first["approval"]["sequence"], first["notification"]["sequence"])
        self.assertLess(first["notification"]["sequence"], second["approval"]["sequence"])
        self.assertEqual(len(self.approvals.list_inbox(owner="human")), 1)
        self.assertEqual(len(self.approvals.list_inbox(owner="master")), 1)

        self.store.close()
        self.store = build_store(self.root)
        self.store.start()
        restarted = ApprovalInboxService(self.store)
        after_restart = restarted.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_human_approval",
            capabilities=["approve_taskbook"],
            context={"phase": "confirmation_build"},
            operation_id="job_first",
        )
        self.assertEqual(after_restart["approval"]["approval_id"], first["approval"]["approval_id"])
        self.assertEqual(len(restarted.list_inbox(owner="human")), 1)

    def test_resolution_and_read_handled_reducers_are_revisioned_and_idempotent(self) -> None:
        created = self.approvals.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_human_approval",
            capabilities=["approve_taskbook"],
            context={},
            operation_id="job_resolution",
        )
        approval_id = created["approval"]["approval_id"]
        inbox = self.approvals.list_inbox(owner="human")[0]

        read_envelope = envelope("read-approval-item", inbox["store_revision"])
        read = self.approvals.update_inbox_status(inbox["inbox_id"], "READ", read_envelope)
        self.assertEqual(
            self.approvals.update_inbox_status(inbox["inbox_id"], "READ", read_envelope),
            read,
        )
        handled = self.approvals.update_inbox_status(
            inbox["inbox_id"],
            "HANDLED",
            envelope("handle-approval-item", read["inbox_revision"]),
        )
        self.assertEqual(handled["item"]["status"], "HANDLED")
        self.assertEqual(handled["item"]["read_at"], read["item"]["read_at"])

        with self.assertRaises(HarnessError) as wrong_owner:
            self.approvals.commit_resolution(
                approval_id,
                "APPROVED",
                envelope("master-cannot-resolve", created["approval_revision"], "master"),
            )
        self.assertEqual(wrong_owner.exception.code, "VALIDATION_ERROR")

        decision = envelope("resolve-once", created["approval_revision"])
        resolved = self.approvals.commit_resolution(approval_id, "APPROVED", decision)
        self.assertEqual(
            self.approvals.commit_resolution(approval_id, "APPROVED", decision),
            resolved,
        )
        self.assertEqual(resolved["approval"]["status"], "APPROVED")
        with self.assertRaises(HarnessError) as duplicate:
            self.approvals.commit_resolution(
                approval_id,
                "REJECTED",
                envelope("different-decision", resolved["approval_revision"]),
            )
        self.assertEqual(duplicate.exception.code, "INVALID_STATE_TRANSITION")

    def test_instance_view_is_the_only_read_trigger_and_updates_global_count(self) -> None:
        created = self.approvals.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_human_approval",
            capabilities=["approve_taskbook"],
            context={},
            operation_id="job_view",
        )
        self.assertEqual(self.approvals.unread_count(owner="human"), 1)

        viewed = self.approvals.mark_instance_notifications_read("i_image_1")

        self.assertEqual([item["status"] for item in viewed], ["READ"])
        self.assertEqual(self.approvals.unread_count(owner="human"), 0)
        self.assertEqual(self.approvals.mark_instance_notifications_read("i_image_1"), [])
        approval = self.approvals.get_approval(created["approval"]["approval_id"])
        self.assertEqual(approval["approval"]["status"], "PENDING")

    def test_workbench_progress_closes_the_previous_workflow_gate(self) -> None:
        created = self.approvals.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_human_approval",
            capabilities=["approve_taskbook"],
            context={},
            operation_id="job_external",
        )

        resolved = self.approvals.acknowledge_external_workflow_approvals(
            "t_approvals",
            "i_image_1",
            current_step_id="next_step",
            current_operation_id="job_next",
            actor_id="image_adapter",
        )

        self.assertEqual([item["status"] for item in resolved], ["APPROVED"])
        approval = self.approvals.get_approval(created["approval"]["approval_id"])
        self.assertEqual(approval["approval"]["status"], "APPROVED")
        inbox = self.approvals.list_inbox(owner="human")
        self.assertEqual([item["status"] for item in inbox], ["HANDLED"])

    def test_workbench_progress_repairs_notification_after_interrupted_resolution(self) -> None:
        created = self.approvals.ensure_workflow_approval(
            "t_approvals",
            "i_image_1",
            step_id="waiting_human_approval",
            capabilities=["approve_taskbook"],
            context={},
            operation_id="job_interrupted",
        )
        approval = created["approval"]
        externally_resolved = {
            **approval,
            "status": "APPROVED",
            "revision": approval["revision"] + 1,
            "resolved_at": approval["created_at"],
            "resolved_by_type": "adapter",
            "resolved_by_id": "image_adapter",
        }
        self.store.approval.put(
            "t_approvals",
            approval["approval_id"],
            externally_resolved,
            expected_revision=created["approval_revision"],
            actor=Actor("adapter", "image_adapter"),
            command="simulate_interrupted_external_resolution",
            idempotency_key="simulate-interrupted-external-resolution",
        )

        resolved = self.approvals.acknowledge_external_workflow_approvals(
            "t_approvals",
            "i_image_1",
            current_step_id="next_step",
            current_operation_id="job_next",
            actor_id="image_adapter",
        )

        self.assertEqual(resolved, [])
        inbox = self.approvals.list_inbox(owner="human")
        self.assertEqual([item["status"] for item in inbox], ["HANDLED"])

    def test_unread_count_is_not_limited_to_the_first_inbox_page(self) -> None:
        for index in range(55):
            self.approvals.ensure_notification(
                "t_approvals",
                kind="INSTANCE_SUCCEEDED",
                owner="human",
                title="子任务完成",
                message=f"第 {index + 1} 个子任务通知。",
                deep_link="instances/i_image_1",
                dedupe_key=f"count-{index}",
                instance_id="i_image_1",
            )

        self.assertEqual(len(self.approvals.list_inbox(owner="human", limit=50)), 50)
        self.assertEqual(self.approvals.unread_count(owner="human"), 55)

if __name__ == "__main__":
    unittest.main()
