"""Monitoring, reconciliation and terminal process lifecycle operations."""

from __future__ import annotations

import os
import threading
import time
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ..core.errors import HarnessError
from ..core.logging import redact
from ..domain.commands import CommandEnvelope
from ..storage.atomic import atomic_write_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
from .process_runtime import ACTIVE_LAUNCH_STATES, tail_lines


class SupervisorLifecycleMixin:
    """Lifecycle extension point independent from process construction."""

    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    def monitor_once(self) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for record_path in sorted(self.launch_root.glob("*.json")):
            record = read_json(record_path)
            with self._instance_thread_lock(record["task_id"], record["instance_id"]):
                record = read_json(record_path)
                if record["state"] not in {"STARTING", "RUNNING"}:
                    continue
                alive = self._record_is_alive(record)
                if alive and record["state"] == "STARTING":
                    if self._probe(record["port"], record["health_path"]) and self._probe(
                        record["port"], record["readiness_path"]
                    ):
                        changes.append(self._promote_ready(record))
                        continue
                    if self._startup_elapsed(record) <= record["startup_timeout_seconds"]:
                        continue
                if alive and record["state"] == "RUNNING":
                    if self._probe(record["port"], record["health_path"]):
                        if record.get("health_failures"):
                            record["health_failures"] = 0
                            atomic_write_json(record_path, record)
                        continue
                    record["health_failures"] = record.get("health_failures", 0) + 1
                    atomic_write_json(record_path, record)
                    if record["health_failures"] < 3:
                        continue
                elif alive:
                    continue
                changes.append(
                    self._handle_unexpected_exit(record, "health_or_process_exit")
                )
        return changes

    def reconcile(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        active_instances: set[tuple[str, str]] = set()
        records = [read_json(path) for path in sorted(self.launch_root.glob("*.json"))]
        self.port_allocator.reconcile(
            {
                item["launch_id"]: item
                for item in records
                if item["state"] in ACTIVE_LAUNCH_STATES
            }
        )
        for record in records:
            if record["state"] not in ACTIVE_LAUNCH_STATES:
                continue
            record = self._hydrate_launch_identity(record)
            key = (record["task_id"], record["instance_id"])
            active_instances.add(key)
            if record.get("pid") and self._record_is_alive(record):
                self.port_allocator.restore(
                    record["task_id"],
                    record["instance_id"],
                    record["launch_id"],
                    record["port"],
                )
                if record["state"] == "STARTING" and self._probe(
                    record["port"], record["health_path"]
                ) and self._probe(record["port"], record["readiness_path"]):
                    results.append(self._promote_ready(record))
                    continue
                process_state = "RUNNING" if record["state"] == "RUNNING" else "STARTING"
                self._write_process_projection(record, process_state)
                results.append({"launch_id": record["launch_id"], "status": "RECOVERED"})
            else:
                results.append(self._handle_unexpected_exit(record, "startup_reconcile"))
        for instance in self._all_instances():
            key = (instance["task_id"], instance["instance_id"])
            if instance["status"] in {"STARTING", "RUNNING"} and key not in active_instances:
                self._interrupt_model_calls(*key, "missing_launch_on_recovery")
                self._transition(*key, "CRASHED", f"reconcile-{instance['instance_id']}")
                results.append(
                    {
                        "task_id": key[0],
                        "instance_id": key[1],
                        "status": "CRASHED",
                    }
                )
        return results

    def cancel_instance(
        self, task_id: str, instance_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        with self._instance_thread_lock(task_id, instance_id):
            self._instance(task_id, instance_id)
            active = self._active_launch_for_instance(task_id, instance_id)
            if active is not None:
                self._interrupt_model_calls(task_id, instance_id, "cancelled")
                self._stop_launch(active, "CANCELLED")
            self._transition(
                task_id,
                instance_id,
                "CANCELLED",
                idempotency_key or f"cancel-{instance_id}",
            )
            return deepcopy(self._instance(task_id, instance_id))

    def archive_instance(
        self, task_id: str, instance_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        with self._instance_thread_lock(task_id, instance_id):
            active = self._active_launch_for_instance(task_id, instance_id)
            if active is not None:
                self._stop_launch(active, "ARCHIVED")
            self._transition(
                task_id,
                instance_id,
                "ARCHIVED",
                idempotency_key or f"archive-{instance_id}",
            )
            root = self.store.layout.initialize_instance(task_id, instance_id)
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_symlink():
                    continue
                os.chmod(path, 0o500 if path.is_dir() else 0o400)
            os.chmod(root, 0o500)
            return deepcopy(self._instance(task_id, instance_id))

    def begin_model_call(
        self, task_id: str, instance_id: str, *, attempt_id: str, request_id: str
    ) -> dict[str, Any]:
        validate_identifier(attempt_id, "attempt_id")
        if not request_id or len(request_id) > 512 or "\x00" in request_id:
            raise HarnessError("VALIDATION_ERROR", "The Provider request id is invalid.")
        path = self._attempt_path(task_id, attempt_id)
        with self._instance_thread_lock(task_id, instance_id), FileLock(
            self._attempt_lock(task_id, attempt_id), self.store.lock_timeout_seconds
        ):
            active = self._active_launch_for_instance(task_id, instance_id)
            if active is None or active["state"] != "RUNNING":
                raise HarnessError(
                    "PROCESS_START_FAILED", "The instance has no ready process."
                )
            if path.exists():
                existing = read_json(path)
                if (
                    existing["request_id"] != request_id
                    or existing["instance_id"] != instance_id
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The attempt id was reused for another model call.",
                    )
                return existing
            record = {
                "attempt_id": attempt_id,
                "request_id": request_id,
                "task_id": task_id,
                "instance_id": instance_id,
                "launch_id": active["launch_id"],
                "status": "IN_PROGRESS",
                "started_at": utc_now(),
            }
            atomic_write_json(path, record)
            return record

    def complete_model_call(self, task_id: str, attempt_id: str) -> dict[str, Any]:
        path = self._attempt_path(task_id, attempt_id)
        with FileLock(self._attempt_lock(task_id, attempt_id), self.store.lock_timeout_seconds):
            if not path.exists():
                raise HarnessError("VALIDATION_ERROR", "The model-call attempt does not exist.")
            record = read_json(path)
            if record["status"] == "IN_PROGRESS":
                record.update({"status": "COMPLETED", "completed_at": utc_now()})
                atomic_write_json(path, record)
            return record

    def log_summary(
        self, task_id: str, instance_id: str, *, max_lines: int = 100
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        self._instance(task_id, instance_id)
        if max_lines < 1 or max_lines > 1000:
            raise HarnessError("VALIDATION_ERROR", "The log summary limit is invalid.")
        root = (
            self.store.layout.workspace_root
            / "tasks"
            / task_id
            / "instances"
            / instance_id
            / "logs"
        )
        result: dict[str, list[str]] = {}
        for stream in ("stdout", "stderr"):
            path = root / f"{stream}.log"
            lines = tail_lines(
                path,
                max_lines,
                trusted_root=self.store.layout.workspace_root,
            )
            result[stream] = [str(redact(line)) for line in lines[-max_lines:]]
        return {"task_id": task_id, "instance_id": instance_id, "logs": result}

    def start_monitoring(self, interval_seconds: float = 1.0) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()

        def loop() -> None:
            while not self._monitor_stop.wait(interval_seconds):
                try:
                    self.monitor_once()
                except Exception as exc:
                    self.logger.error(
                        "process_monitor_iteration_failed",
                        extra={"fields": {"exception_type": type(exc).__name__}},
                    )

        self._monitor_thread = threading.Thread(
            target=loop, name="harness-process-monitor", daemon=True
        )
        self._monitor_thread.start()

    def close(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=2)
        self._monitor_thread = None

    def _stop_launch(self, record: dict[str, Any], reason: str) -> dict[str, Any]:
        grace = float(record["shutdown_grace_seconds"])
        if record.get("pid") and (
            self._record_is_alive(record) or self._record_child_is_alive(record)
        ):
            self._terminate_group(record["pid"], grace)
        process = self._children.pop(record["launch_id"], None)
        exit_code = process.poll() if process is not None else None
        record.update(
            {
                "state": "EXITED",
                "exit_code": exit_code,
                "exit_reason": reason,
                "exited_at": utc_now(),
            }
        )
        atomic_write_json(self._launch_path(record["launch_id"]), record)
        self.port_allocator.release(record["launch_id"], record["port"])
        self._write_process_projection(record, "EXITED")
        self._append_process_event(record["task_id"], "PROCESS_STOPPED", record)
        return record

    def _handle_unexpected_exit(
        self, record: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        if record.get("pid") and (
            self._record_is_alive(record) or self._record_child_is_alive(record)
        ):
            self._terminate_group(record["pid"], 0.1)
        process = self._children.pop(record["launch_id"], None)
        exit_code = process.poll() if process is not None else None
        record.update(
            {
                "state": "EXITED",
                "exit_code": exit_code,
                "exit_reason": reason,
                "exited_at": utc_now(),
            }
        )
        atomic_write_json(self._launch_path(record["launch_id"]), record)
        self.port_allocator.release(record["launch_id"], record["port"])
        self._write_process_projection(record, "EXITED")
        self._interrupt_model_calls(record["task_id"], record["instance_id"], reason)
        instance = self._instance(record["task_id"], record["instance_id"])
        event_type = "PROCESS_CRASHED"
        result_status = "CRASHED"
        if instance["status"] == "STARTING":
            result_status = "FAILED_TO_START"
            event_type = "PROCESS_START_FAILED"
            self._transition(
                record["task_id"],
                record["instance_id"],
                "FAILED_TO_START",
                f"{record['launch_id']}-failed-during-start",
            )
        elif instance["status"] == "RUNNING":
            self._transition(
                record["task_id"],
                record["instance_id"],
                "CRASHED",
                f"{record['launch_id']}-crashed",
            )
        elif instance["status"] == "WAITING_APPROVAL":
            self._transition(
                record["task_id"],
                record["instance_id"],
                "FAILED",
                f"{record['launch_id']}-crashed-waiting",
            )
            result_status = "FAILED"
        self._append_process_event(record["task_id"], event_type, record)
        return {
            "launch_id": record["launch_id"],
            "instance_id": record["instance_id"],
            "status": result_status,
        }

    def _transition(
        self, task_id: str, instance_id: str, target: str, idempotency_key: str
    ) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        if instance["status"] == target:
            return instance
        plan = self.store.plan.get(task_id, task_id)
        planned = plan is not None and any(
            item["instance_id"] == instance_id for item in plan["instances"]
        )
        if planned:
            deadline = time.monotonic() + self.store.lock_timeout_seconds
            while True:
                revision = self.store.task.revision(task_id, task_id)
                try:
                    result = self.commands.transition_instance(
                        task_id,
                        instance_id,
                        target,
                        CommandEnvelope(
                            idempotency_key=idempotency_key,
                            actor_type="system",
                            actor_id="process_supervisor",
                            expected_revision=revision,
                        ),
                    )
                    break
                except HarnessError as exc:
                    current = self._instance(task_id, instance_id)
                    if current["status"] == target:
                        return current
                    if exc.code != "REVISION_CONFLICT" or time.monotonic() >= deadline:
                        raise
            return next(
                item
                for item in result["plan"]["instances"]
                if item["instance_id"] == instance_id
            )
        self.commands.machine.transition("agent_instance", instance["status"], target)
        updated = {**instance, "status": target}
        self.store.instance.put(
            task_id,
            instance_id,
            updated,
            expected_revision=self.store.instance.revision(task_id, instance_id),
            actor=Actor("system", "process_supervisor"),
            command="transition_unplanned_instance",
            idempotency_key=idempotency_key,
        )
        return updated
