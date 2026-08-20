from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.core.errors import HarnessError
from harness.services.approvals import ApprovalInboxService
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

if __name__ == "__main__":
    unittest.main()
