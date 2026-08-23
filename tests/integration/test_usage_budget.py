from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from harness.core.errors import HarnessError
from harness.services.approvals import ApprovalInboxService
from harness.services.retry_budget import RetryBudgetService
from harness.services.usage import UsageService
from harness.storage.repository import Actor
from runtime_helpers import (
    build_service,
    create_task,
    envelope,
    image_plan,
    register_model_call_attempt,
)


class EmptyUsageAdapter:
    agent_type = "image"

    def collect_usage(self, instance_id, cursor):
        return []


class UsageAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store, self.commands = build_service(self.root)
        create_task(self.commands, "t_g4")
        self.commands.save_plan(
            "t_g4",
            **image_plan("t_g4"),
            envelope=envelope("save-g4", 1),
        )
        register_model_call_attempt(
            self.store, "t_g4", "i_image_1", "attempt_initial"
        )
        self.approvals = ApprovalInboxService(self.store)
        self.usage = UsageService(self.store)
        self.budgets = RetryBudgetService(self.store, self.approvals)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def usage_event(self, event_id: str = "usage_g4_1") -> dict:
        return {
            "schema_version": "1.0",
            "event_id": event_id,
            "task_id": "t_g4",
            "instance_id": "i_image_1",
            "agent_type": "image",
            "request_id": f"request_{event_id}",
            "model": "image-model",
            "input_tokens": 10,
            "output_tokens": 5,
            "cached_input_tokens": 2,
            "reasoning_tokens": 1,
            "total_tokens": 15,
            "cost_micros": 45,
            "price_catalog_revision": "price_v1",
            "occurred_at": "2026-08-21T02:00:00Z",
        }

    def test_usage_is_owned_idempotent_aggregated_and_explicit_when_missing(self) -> None:
        missing = self.usage.collect_instance("t_g4", "i_image_1", EmptyUsageAdapter())
        self.assertEqual(missing["completeness"], "NOT_REPORTED")
        self.assertEqual(self.usage.summary("t_g4")["tokens"]["total_tokens"], 0)

        first = self.usage.ingest(
            "t_g4",
            "i_image_1",
            [self.usage_event()],
            source="internal",
            collection_complete=True,
        )
        replay = self.usage.ingest(
            "t_g4",
            "i_image_1",
            [self.usage_event()],
            source="internal",
            collection_complete=True,
        )
        self.assertEqual(first["accepted"], 1)
        self.assertEqual(replay["duplicates"], 1)
        summary = self.usage.summary("t_g4")
        self.assertEqual(summary["completeness"], "COMPLETE")
        self.assertEqual(summary["tokens"]["total_tokens"], 15)
        self.assertEqual(summary["cost"]["known_micros"], 45)
        self.assertEqual(summary["models"][0]["model"], "image-model")
        self.assertEqual(summary["time_buckets"][0]["hour"], "2026-08-21T02:00:00Z")

        changed = self.usage_event()
        changed["request_id"] = "different_request"
        with self.assertRaises(HarnessError) as conflict:
            self.usage.ingest("t_g4", "i_image_1", [changed], source="internal")
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

        wrong_owner = self.usage_event("usage_wrong_owner")
        wrong_owner["task_id"] = "t_other"
        with self.assertRaises(HarnessError) as invalid:
            self.usage.ingest("t_g4", "i_image_1", [wrong_owner], source="internal")
        self.assertEqual(invalid.exception.code, "VALIDATION_ERROR")

        valid_leader = self.usage_event("usage_valid_leader")
        with self.assertRaises(HarnessError):
            self.usage.ingest(
                "t_g4",
                "i_image_1",
                [valid_leader, wrong_owner],
                source="internal",
            )
        self.assertEqual(self.usage.summary("t_g4")["event_count"], 1)

    def test_usage_recovery_preserves_an_opaque_adapter_cursor(self) -> None:
        self.usage.ingest(
            "t_g4",
            "i_image_1",
            [self.usage_event()],
            source="adapter",
            cursor="provider_page_token_42",
            collection_complete=False,
        )
        self.assertEqual(self.usage.recover(), [])
        collected = self.usage.ingest(
            "t_g4",
            "i_image_1",
            [],
            source="adapter",
            collection_complete=False,
        )
        self.assertEqual(collected["cursor"], "provider_page_token_42")

    def test_usage_v1_1_preserves_non_token_image_billing_units(self) -> None:
        event = {
            "schema_version": "1.1",
            "event_id": "usage_image_unit",
            "task_id": "t_g4",
            "instance_id": "i_image_1",
            "agent_type": "image",
            "request_id": "request_image_unit",
            "provider_request_id": None,
            "provider": "ark",
            "model": "seedream",
            "call_type": "text_to_image_model",
            "usage_basis": "image_units",
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "billing_units": [
                {
                    "unit": "image",
                    "quantity": 1,
                    "attributes": {
                        "resolution": "2560x1440",
                        "model_tier": "seedream",
                    },
                }
            ],
            "raw_usage": {},
            "occurred_at": "2026-08-21T02:00:00Z",
        }
        ingested = self.usage.ingest(
            "t_g4", "i_image_1", [event], source="adapter", collection_complete=True
        )
        summary = self.usage.summary("t_g4")

        self.assertEqual(ingested["accepted"], 1)
        self.assertEqual(summary["completeness"], "COMPLETE")
        self.assertEqual(summary["tokens"]["total_tokens"], 0)
        self.assertEqual(summary["events"][0]["billing_units"][0]["unit"], "image")
        self.assertEqual(summary["cost"]["completeness"], "UNKNOWN")

    def test_parallel_retry_reservations_cannot_cross_count_or_token_limits(self) -> None:
        policy = {
            "max_auto_retries_per_retry_group": 2,
            "max_auto_retry_tokens_task": 200,
            "retry_token_reservation_by_agent": {"image": 100},
            "max_auto_retry_cost_micros": None,
            "price_catalog_revision": None,
        }
        configured = self.budgets.configure(
            "t_g4",
            policy,
            expected_revision=0,
            idempotency_key="configure-g4",
            actor=Actor("human", "operator"),
        )
        self.assertEqual(configured["revision"], 1)

        def reserve(index: int):
            try:
                return self.budgets.request_retry(
                    "t_g4",
                    "i_image_1",
                    attempt_id=f"attempt_retry_{index}",
                    retry_of_attempt_id="attempt_initial",
                    idempotency_key=f"reserve-retry-{index}",
                    actor=Actor("master", "master_default"),
                )
            except HarnessError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(reserve, range(1, 4)))
        allowed = [item for item in results if isinstance(item, dict)]
        denied = [item for item in results if isinstance(item, HarnessError)]
        self.assertEqual(len(allowed), 2)
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].code, "BUDGET_GATE_DENIED")
        budget = self.budgets.get("t_g4")
        self.assertEqual(budget["retry_budget_ledger"]["retry_tokens_reserved"], 200)
        self.assertEqual(
            sum(item["status"] == "PENDING_APPROVAL" for item in budget["attempts"]),
            1,
        )
        approval_id = denied[0].details["approval_id"]
        approval = self.approvals.get_approval(approval_id)
        self.assertEqual(approval["approval"]["owner"], "human")
        self.assertEqual(approval["approval"]["kind"], "BUDGET_OVERRIDE")

    def test_unknown_cost_needs_human_limits_and_override_is_consumed_once(self) -> None:
        self.budgets.configure(
            "t_g4",
            {
                "max_auto_retries_per_retry_group": 2,
                "max_auto_retry_tokens_task": 500,
                "retry_token_reservation_by_agent": {"image": 100},
                "max_auto_retry_cost_micros": 1_000,
                "price_catalog_revision": "price_v1",
            },
            expected_revision=0,
            idempotency_key="configure-cost-g4",
            actor=Actor("human", "operator"),
        )
        with self.assertRaises(HarnessError) as denied:
            self.budgets.request_retry(
                "t_g4",
                "i_image_1",
                attempt_id="attempt_cost_retry",
                retry_of_attempt_id="attempt_initial",
                idempotency_key="reserve-cost-retry",
                actor=Actor("master", "master_default"),
            )
        approval_id = denied.exception.details["approval_id"]
        details = self.approvals.get_approval(approval_id)
        with self.assertRaises(HarnessError) as missing_limit:
            self.budgets.resolve_approval(
                approval_id,
                decision="APPROVED",
                action="approve_once",
                payload={},
                envelope=envelope(
                    "approve-cost-without-limit",
                    details["approval_revision"],
                    "human",
                    "operator",
                ),
            )
        self.assertEqual(missing_limit.exception.code, "VALIDATION_ERROR")
        approved = self.budgets.resolve_approval(
            approval_id,
            decision="APPROVED",
            action="approve_once",
            payload={"cost_limit_micros": 600},
            envelope=envelope(
                "approve-cost-once",
                details["approval_revision"],
                "human",
                "operator",
            ),
        )
        self.assertEqual(approved["attempt"]["status"], "AUTHORIZED")
        with self.assertRaises(HarnessError) as master_bypass:
            self.budgets.consume_override(
                "t_g4",
                "attempt_cost_retry",
                idempotency_key="master-cannot-consume",
                actor=Actor("master", "master_default"),
            )
        self.assertEqual(master_bypass.exception.code, "VALIDATION_ERROR")
        consumed = self.budgets.consume_override(
            "t_g4",
            "attempt_cost_retry",
            idempotency_key="consume-cost-once",
            actor=Actor("system", "retry_executor"),
        )
        replay = self.budgets.consume_override(
            "t_g4",
            "attempt_cost_retry",
            idempotency_key="consume-cost-replay",
            actor=Actor("system", "retry_executor"),
        )
        self.assertEqual(consumed, replay)
        with self.assertRaises(HarnessError) as wrong_adapter:
            self.budgets.settle(
                "t_g4",
                "attempt_cost_retry",
                actual_tokens=150,
                actual_cost_micros=700,
                idempotency_key="wrong-adapter-settle",
                actor=Actor("adapter", "ppt_adapter"),
            )
        self.assertEqual(wrong_adapter.exception.code, "VALIDATION_ERROR")
        settled = self.budgets.settle(
            "t_g4",
            "attempt_cost_retry",
            actual_tokens=150,
            actual_cost_micros=700,
            idempotency_key="settle-cost-retry",
            actor=Actor("adapter", "image_adapter"),
        )
        self.assertEqual(settled["attempt"]["status"], "EXCEEDED")
        self.assertTrue(settled["ledger"]["frozen"])

    def test_budget_resolution_recovers_after_approval_commit_interruption(self) -> None:
        self.budgets.configure(
            "t_g4",
            {
                "max_auto_retries_per_retry_group": 1,
                "max_auto_retry_tokens_task": 100,
                "retry_token_reservation_by_agent": {"image": 100},
                "max_auto_retry_cost_micros": 1_000,
                "price_catalog_revision": "price_v1",
            },
            expected_revision=0,
            idempotency_key="configure-recovery-g4",
            actor=Actor("human", "operator"),
        )
        with self.assertRaises(HarnessError) as denied:
            self.budgets.request_retry(
                "t_g4",
                "i_image_1",
                attempt_id="attempt_recovery_retry",
                retry_of_attempt_id="attempt_initial",
                idempotency_key="reserve-recovery-retry",
                actor=Actor("master", "master_default"),
            )
        approval_id = denied.exception.details["approval_id"]
        details = self.approvals.get_approval(approval_id)
        command = envelope(
            "approve-recovery-once",
            details["approval_revision"],
            "human",
            "operator",
        )
        original_commit = self.approvals.commit_resolution

        def commit_then_interrupt(*args, **kwargs):
            original_commit(*args, **kwargs)
            raise RuntimeError("simulated process interruption")

        with (
            patch.object(
                self.approvals,
                "commit_resolution",
                side_effect=commit_then_interrupt,
            ),
            self.assertRaisesRegex(RuntimeError, "simulated process interruption"),
        ):
            self.budgets.resolve_approval(
                approval_id,
                decision="APPROVED",
                action="approve_once",
                payload={"cost_limit_micros": 600},
                envelope=command,
            )

        pending = self.budgets.get("t_g4")["attempts"][0]
        self.assertEqual(pending["status"], "PENDING_APPROVAL")
        self.assertIsNotNone(pending["pending_resolution"])
        recovered = self.budgets.recover()
        self.assertTrue(recovered[0]["resolution_completed"])
        completed = self.budgets.get("t_g4")["attempts"][0]
        self.assertEqual(completed["status"], "AUTHORIZED")
        self.assertEqual(completed["reserved_cost_micros"], 600)
        self.assertIsNone(completed["pending_resolution"])

        replay = self.budgets.resolve_approval(
            approval_id,
            decision="APPROVED",
            action="approve_once",
            payload={"cost_limit_micros": 600},
            envelope=command,
        )
        self.assertEqual(replay["attempt"]["status"], "AUTHORIZED")
        with self.assertRaises(HarnessError) as changed:
            self.budgets.resolve_approval(
                approval_id,
                decision="APPROVED",
                action="approve_once",
                payload={"cost_limit_micros": 601},
                envelope=command,
            )
        self.assertEqual(changed.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_retry_replay_and_replacement_instance_retain_lineage(self) -> None:
        create_task(self.commands, "t_g4_replacement")
        self.commands.save_plan(
            "t_g4_replacement",
            **image_plan("t_g4_replacement", count=2),
            envelope=envelope("save-g4-replacement", 1),
        )
        register_model_call_attempt(
            self.store,
            "t_g4_replacement",
            "i_image_1",
            "attempt_original",
        )
        self.budgets.configure(
            "t_g4_replacement",
            {
                "max_auto_retries_per_retry_group": 2,
                "max_auto_retry_tokens_task": 200,
                "retry_token_reservation_by_agent": {"image": 100},
                "max_auto_retry_cost_micros": None,
                "price_catalog_revision": None,
            },
            expected_revision=0,
            idempotency_key="configure-g4-replacement",
            actor=Actor("human", "operator"),
        )
        request = {
            "attempt_id": "attempt_replacement_1",
            "retry_of_attempt_id": "attempt_original",
            "idempotency_key": "reserve-replacement-1",
            "actor": Actor("master", "master_default"),
        }
        first = self.budgets.request_retry("t_g4_replacement", "i_image_1", **request)
        replay = self.budgets.request_retry("t_g4_replacement", "i_image_1", **request)
        self.assertEqual(first, replay)

        replacement = self.budgets.request_retry(
            "t_g4_replacement",
            "i_image_2",
            attempt_id="attempt_replacement_2",
            retry_of_attempt_id="attempt_replacement_1",
            idempotency_key="reserve-replacement-2",
            actor=Actor("master", "master_default"),
        )
        self.assertTrue(replacement["allowed"])
        self.assertEqual(
            replacement["attempt"]["retry_group_id"],
            first["attempt"]["retry_group_id"],
        )
        self.assertEqual(replacement["attempt"]["root_attempt_id"], "attempt_original")
        self.budgets.settle(
            "t_g4_replacement",
            "attempt_replacement_1",
            actual_tokens=90,
            actual_cost_micros=None,
            idempotency_key="settle-shared-key",
            actor=Actor("adapter", "image_adapter"),
        )
        with self.assertRaises(HarnessError) as reused_key:
            self.budgets.settle(
                "t_g4_replacement",
                "attempt_replacement_2",
                actual_tokens=90,
                actual_cost_micros=None,
                idempotency_key="settle-shared-key",
                actor=Actor("adapter", "image_adapter"),
            )
        self.assertEqual(reused_key.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_unregistered_and_cross_owner_roots_cannot_mint_retry_groups(self) -> None:
        create_task(self.commands, "t_g4_scope")
        self.commands.save_plan(
            "t_g4_scope",
            **image_plan("t_g4_scope", count=2),
            envelope=envelope("save-g4-scope", 1),
        )
        create_task(self.commands, "t_g4_other")
        self.commands.save_plan(
            "t_g4_other",
            **image_plan("t_g4_other"),
            envelope=envelope("save-g4-other", 1),
        )
        register_model_call_attempt(
            self.store,
            "t_g4_scope",
            "i_image_2",
            "attempt_other_instance_root",
        )
        register_model_call_attempt(
            self.store,
            "t_g4_other",
            "i_image_1",
            "attempt_other_task_root",
        )
        register_model_call_attempt(
            self.store,
            "t_g4_scope",
            "i_image_1",
            "attempt_registered_collision",
        )
        register_model_call_attempt(
            self.store,
            "t_g4_scope",
            "i_image_1",
            "attempt_scope_root",
        )

        rejected_roots = (
            "attempt_fabricated_root_one",
            "attempt_fabricated_root_two",
            "attempt_other_instance_root",
            "attempt_other_task_root",
        )
        for index, root_attempt_id in enumerate(rejected_roots, start=1):
            with self.subTest(root_attempt_id=root_attempt_id):
                with self.assertRaises(HarnessError) as rejected:
                    self.budgets.request_retry(
                        "t_g4_scope",
                        "i_image_1",
                        attempt_id=f"attempt_adversarial_retry_{index}",
                        retry_of_attempt_id=root_attempt_id,
                        idempotency_key=f"reserve-adversarial-retry-{index}",
                        actor=Actor("master", "master_default"),
                        reservation_tokens=100,
                    )
                self.assertEqual(rejected.exception.code, "VALIDATION_ERROR")

        self.assertEqual(self.budgets.get("t_g4_scope")["attempts"], [])

        with self.assertRaises(HarnessError) as collision:
            self.budgets.request_retry(
                "t_g4_scope",
                "i_image_1",
                attempt_id="attempt_registered_collision",
                retry_of_attempt_id="attempt_scope_root",
                idempotency_key="reserve-registered-id-collision",
                actor=Actor("master", "master_default"),
                reservation_tokens=100,
            )
        self.assertEqual(collision.exception.code, "IDEMPOTENCY_CONFLICT")
