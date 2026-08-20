from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harness.core.errors import HarnessError
from harness.storage.repository import Actor
from runtime_helpers import build_store


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = build_store(self.root)
        self.store.start()
        self.actor = Actor("system", "repository_test")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_approval_inbox_retry_and_usage_repositories_are_durable(self) -> None:
        approval = {
            "schema_version": "1.0",
            "approval_id": "ap_one",
            "task_id": "t_repositories",
            "instance_id": "i_one",
            "step_id": "style_confirm",
            "kind": "WORKFLOW",
            "owner": "human",
            "status": "PENDING",
            "payload_ref": "approvals/ap_one/request.json",
            "created_at": "2026-08-20T12:00:00Z",
            "sequence": 1,
            "revision": 1,
        }
        self.store.approval.put(
            "t_repositories",
            "ap_one",
            approval,
            expected_revision=0,
            actor=self.actor,
            command="create_approval",
            idempotency_key="approval-one",
        )
        for inbox_id, created_at, sequence in (
            ("inbox_later", "2026-08-20T12:00:01Z", 2),
            ("inbox_first", "2026-08-20T12:00:00Z", 1),
        ):
            self.store.inbox.put(
                "t_repositories",
                inbox_id,
                {
                    "schema_version": "1.0",
                    "inbox_id": inbox_id,
                    "task_id": "t_repositories",
                    "instance_id": "i_one",
                    "approval_id": "ap_one",
                    "kind": "APPROVAL_REQUIRED",
                    "owner": "human",
                    "created_at": created_at,
                    "sequence": sequence,
                    "status": "UNREAD",
                    "title": "Approval required",
                    "message": "The Image workflow is waiting for a decision.",
                    "deep_link": "inbox/ap_one",
                    "revision": 1,
                    "dedupe_key": f"approval:{inbox_id}",
                },
                expected_revision=0,
                actor=self.actor,
                command="create_inbox",
                idempotency_key=inbox_id,
            )
        self.store.retry_budget.put(
            "t_repositories",
            "t_repositories",
            {"revision": 1, "auto_retries_started": 0},
            expected_revision=0,
            actor=self.actor,
            command="create_retry_budget",
            idempotency_key="retry-budget",
        )
        usage = {
            "schema_version": "1.0",
            "event_id": "usage_one",
            "task_id": "t_repositories",
            "instance_id": "i_one",
            "agent_type": "image",
            "request_id": "request_one",
            "model": "fake-image-model",
            "credential_pair_ref": "cred_test_01",
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 15,
            "occurred_at": "2026-08-20T12:00:02Z",
        }
        self.assertTrue(self.store.usage.append("t_repositories", usage))
        self.assertFalse(self.store.usage.append("t_repositories", usage))
        self.assertEqual(self.store.usage.list("t_repositories"), [usage])
        self.assertEqual(self.store.approval.get("t_repositories", "ap_one"), approval)
        inbox_index = self.store.layout.control_root / "indexes" / "inbox-index.json"
        index = json.loads(inbox_index.read_text(encoding="utf-8"))
        ids = [item["inbox_id"] for item in index["entries"]]
        self.assertEqual(ids, ["inbox_first", "inbox_later"])

    def test_process_writer_lease_has_a_bounded_conflict(self) -> None:
        second = build_store(self.root)
        with self.assertRaises(HarnessError) as captured:
            second.start()
        self.assertEqual(captured.exception.code, "REVISION_CONFLICT")
        self.assertFalse(second.writer_lease.acquired)
