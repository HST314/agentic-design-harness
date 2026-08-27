from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import harness.storage.ndjson as ndjson
from harness.core.errors import HarnessError, SimulatedCrash
from harness.storage.atomic import atomic_write_json
from harness.storage.ndjson import NdjsonCorruptionError, append_record, recover_records
from harness.storage.repository import Actor
from runtime_helpers import (
    build_service,
    build_store,
    create_task,
    envelope,
    image_plan,
    ppt_plan,
)


def task_payload(task_id: str) -> dict:
    timestamp = "2026-08-20T12:00:00Z"
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "title": "Crash recovery",
        "goal": "Recover exactly one legal state.",
        "master_owner": "master_default",
        "start_policy": "manual",
        "status": "DRAFT",
        "created_at": timestamp,
        "updated_at": timestamp,
        "input_manifest": "inputs/manifests/input.json",
        "plan_revision": 1,
    }


class StateStoreRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = build_store(self.root)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    @staticmethod
    def crash_at(target: str):
        def hook(checkpoint: str) -> None:
            if checkpoint == target:
                raise SimulatedCrash(checkpoint)

        return hook

    def test_event_append_is_replayed_when_snapshot_rename_did_not_happen(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.store.task.put(
                "t_replay",
                "t_replay",
                task_payload("t_replay"),
                expected_revision=0,
                actor=Actor("system", "recovery_test"),
                command="create_task",
                idempotency_key="create-replay",
                crash_hook=self.crash_at("after_event_append"),
            )
        self.assertIsNone(self.store.task.get("t_replay", "t_replay"))
        self.store.recover()
        self.assertEqual(self.store.task.get("t_replay", "t_replay")["status"], "DRAFT")
        self.assertEqual(self.store.task.revision("t_replay", "t_replay"), 1)

    def test_snapshot_without_index_is_recovered_and_index_rebuilt(self) -> None:
        with self.assertRaises(SimulatedCrash):
            self.store.task.put(
                "t_index",
                "t_index",
                task_payload("t_index"),
                expected_revision=0,
                actor=Actor("system", "recovery_test"),
                command="create_task",
                idempotency_key="create-index",
                crash_hook=self.crash_at("after_snapshot_rename"),
            )
        index_path = self.store.layout.control_root / "indexes" / "task-index.json"
        self.store.recover()
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual([item["task_id"] for item in index["tasks"]], ["t_index"])

    def test_plan_commit_reconciles_task_stage_and_instance_projections(self) -> None:
        self.store.close()
        store, service = build_service(self.root)
        self.store = store
        created = create_task(service, "t_aggregate")
        draft = image_plan("t_aggregate")
        saved = service.save_plan(
            "t_aggregate",
            stages=draft["stages"],
            instances=draft["instances"],
            task_cards=draft["task_cards"],
            envelope=envelope("save-aggregate", created["revision"]),
        )
        plan = deepcopy(saved["plan"])
        activated_at = "2026-08-20T12:01:00Z"
        plan["task"]["status"] = "RUNNING"
        plan["task"]["updated_at"] = activated_at
        plan["stages"][0]["requirement_lifecycle"]["first_activated_at"] = activated_at
        plan["instances"][0]["requirement_lifecycle"]["first_activated_at"] = activated_at
        with self.assertRaises(SimulatedCrash):
            store.plan.put(
                "t_aggregate",
                "t_aggregate",
                plan,
                expected_revision=store.plan.revision("t_aggregate", "t_aggregate"),
                actor=Actor("system", "crash_test"),
                command="activate_saved_plan",
                idempotency_key="crash-aggregate",
                crash_hook=self.crash_at("after_snapshot_rename"),
            )
        self.assertEqual(
            store.task.get("t_aggregate", "t_aggregate")["status"], "AWAITING_START_CONFIRMATION"
        )
        store.recover()
        self.assertEqual(store.task.get("t_aggregate", "t_aggregate")["status"], "RUNNING")
        self.assertEqual(
            store.instance.get("t_aggregate", "i_image_1")["requirement_lifecycle"][
                "first_activated_at"
            ],
            activated_at,
        )

    def test_committed_business_result_rebuilds_idempotency_after_crash(self) -> None:
        self.store.close()
        store, service = build_service(self.root)
        self.store = store
        request = {
            "task_id": "t_command_replay",
            "title": "Task t_command_replay",
            "goal": "Verify the Phase 1 control-plane behavior.",
            "master_owner": "master_default",
            "start_policy": "manual",
            "input_manifest": "inputs/manifests/input.json",
        }

        with patch.object(
            store.idempotency,
            "remember_digest",
            side_effect=SimulatedCrash("before_idempotency_projection"),
        ), self.assertRaises(SimulatedCrash):
            create_task(service, "t_command_replay")

        self.assertEqual(store.task.revision("t_command_replay", "t_command_replay"), 1)
        self.assertIsNone(
            store.idempotency.lookup(
                "t_command_replay",
                "create-t_command_replay",
                "create_task",
                request,
            )
        )

        store.close()
        recovered_store, recovered_service = build_service(self.root)
        self.store = recovered_store
        replayed = create_task(recovered_service, "t_command_replay")

        self.assertEqual(replayed["revision"], 1)
        self.assertEqual(replayed["task"]["status"], "DRAFT")
        self.assertEqual(
            recovered_store.task.revision("t_command_replay", "t_command_replay"), 1
        )

    def test_topology_replacement_retires_ghost_projections_during_recovery(self) -> None:
        self.store.close()
        store, service = build_service(self.root)
        self.store = store
        created = create_task(service, "t_topology", "auto")
        old = ppt_plan("t_topology")
        landed = service.save_plan(
            "t_topology",
            stages=old["stages"],
            instances=old["instances"],
            task_cards=old["task_cards"],
            envelope=envelope("save-ppt-topology", created["revision"]),
        )
        failed = service.transition_instance(
            "t_topology",
            "i_ppt_1",
            "FAILED_TO_START",
            envelope("fail-ppt-topology", landed["task_revision"], "adapter"),
        )
        replacement = image_plan("t_topology")
        command = envelope("save-image-topology", failed["task_revision"])

        with patch.object(
            store,
            "retire_plan_projections",
            side_effect=SimulatedCrash("after_authoritative_plan_commit"),
        ), self.assertRaises(SimulatedCrash):
            service.save_plan(
                "t_topology",
                stages=replacement["stages"],
                instances=replacement["instances"],
                task_cards=replacement["task_cards"],
                envelope=command,
            )

        self.assertEqual({item["stage_id"] for item in store.stage.list("t_topology")}, {"s_ppt"})
        self.assertEqual(
            {item["instance_id"] for item in store.instance.list("t_topology")},
            {"i_ppt_1"},
        )

        store.close()
        recovered_store, recovered_service = build_service(self.root)
        self.store = recovered_store
        replayed = recovered_service.save_plan(
            "t_topology",
            stages=replacement["stages"],
            instances=replacement["instances"],
            task_cards=replacement["task_cards"],
            envelope=command,
        )

        self.assertEqual(replayed["task_revision"], 4)
        self.assertEqual(
            {item["stage_id"] for item in recovered_store.stage.list("t_topology")},
            {"s_image"},
        )
        self.assertEqual(
            {item["instance_id"] for item in recovered_store.instance.list("t_topology")},
            {"i_image_1"},
        )

    def test_invalid_ndjson_tail_is_truncated_with_warning(self) -> None:
        path = self.root / "events.ndjson"
        append_record(path, {"event_id": "evt_one"})
        with path.open("ab") as handle:
            handle.write(b'{"event_id":"torn"')
            handle.flush()
            os.fsync(handle.fileno())
        warnings: list[dict] = []
        records = recover_records(path, warnings.append)
        self.assertEqual(records, [{"event_id": "evt_one"}])
        self.assertEqual(warnings[0]["type"], "NDJSON_TAIL_TRUNCATED")
        self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_recovery_waits_for_an_inflight_journal_append(self) -> None:
        path = self.root / "events.ndjson"
        complete_record = self.root / "complete-record.ndjson"
        append_record(path, {"event_id": "evt_one"})
        append_record(complete_record, {"event_id": "evt_two"})
        second_line = complete_record.read_bytes()
        split = len(second_line) // 2

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with ndjson._journal_guard(path):
                with path.open("ab") as handle:
                    handle.write(second_line[:split])
                    handle.flush()
                    os.fsync(handle.fileno())
                future = executor.submit(recover_records, path)
                time.sleep(0.02)
                self.assertFalse(future.done())
                with path.open("ab") as handle:
                    handle.write(second_line[split:])
                    handle.flush()
                    os.fsync(handle.fileno())
            records = future.result(timeout=1)
        finally:
            executor.shutdown(wait=True)

        self.assertEqual(
            [record["event_id"] for record in records],
            ["evt_one", "evt_two"],
        )

    def test_interior_ndjson_corruption_is_fatal(self) -> None:
        path = self.root / "events.ndjson"
        append_record(path, {"event_id": "evt_one"})
        with path.open("ab") as handle:
            handle.write(b"{}\n")
        append_record(path, {"event_id": "evt_two"})
        with self.assertRaises(NdjsonCorruptionError):
            recover_records(path)

    def test_snapshot_without_commit_event_is_rejected_as_ghost(self) -> None:
        self.store.layout.initialize_task("t_ghost")
        path = self.store.task.path("t_ghost", "t_ghost")
        atomic_write_json(
            path,
            {
                "store_version": "1.0",
                "object_type": "task",
                "object_id": "t_ghost",
                "revision": 1,
                "payload": task_payload("t_ghost"),
                "committed_at": "2026-08-20T12:00:00Z",
            },
        )
        with self.assertRaises(RuntimeError):
            self.store.recover()

    def test_same_revision_snapshot_drift_is_rebuilt_from_event(self) -> None:
        self.store.task.put(
            "t_drift",
            "t_drift",
            task_payload("t_drift"),
            expected_revision=0,
            actor=Actor("system", "recovery_test"),
            command="create_task",
            idempotency_key="create-drift",
        )
        path = self.store.task.path("t_drift", "t_drift")
        wrapper = json.loads(path.read_text(encoding="utf-8"))
        wrapper["payload"]["title"] = "Uncommitted drift"
        atomic_write_json(path, wrapper)

        warnings = self.store.recover()

        self.assertEqual(self.store.task.get("t_drift", "t_drift")["title"], "Crash recovery")
        self.assertEqual(warnings[0]["type"], "SNAPSHOT_REBUILT")

    def test_failed_start_releases_the_writer_lease(self) -> None:
        self.store.layout.initialize_task("t_bad_start")
        atomic_write_json(
            self.store.task.path("t_bad_start", "t_bad_start"),
            {
                "store_version": "1.0",
                "object_type": "task",
                "object_id": "t_bad_start",
                "revision": 1,
                "payload": task_payload("t_bad_start"),
                "committed_at": "2026-08-20T12:00:00Z",
            },
        )

        with self.assertRaises(RuntimeError):
            self.store.start()

        self.assertFalse(self.store.writer_lease.acquired)

    def test_revision_and_idempotency_conflicts_are_explicit(self) -> None:
        self.store.task.put(
            "t_conflict",
            "t_conflict",
            task_payload("t_conflict"),
            expected_revision=0,
            actor=Actor("system", "test"),
            command="create_task",
            idempotency_key="create-conflict",
        )
        with self.assertRaises(HarnessError) as revision:
            self.store.task.put(
                "t_conflict",
                "t_conflict",
                task_payload("t_conflict"),
                expected_revision=0,
                actor=Actor("system", "test"),
                command="update_task",
                idempotency_key="stale",
            )
        self.assertEqual(revision.exception.code, "REVISION_CONFLICT")
        request = {"value": 1}
        self.store.idempotency.remember("t_conflict", "same-key", "command", request, {"ok": True})
        self.assertEqual(
            self.store.idempotency.lookup("t_conflict", "same-key", "command", request),
            {"ok": True},
        )
        with self.assertRaises(HarnessError) as idempotency:
            self.store.idempotency.lookup("t_conflict", "same-key", "command", {"value": 2})
        self.assertEqual(idempotency.exception.code, "IDEMPOTENCY_CONFLICT")
