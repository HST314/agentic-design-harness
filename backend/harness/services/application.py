"""Recoverable application use cases above domain and infrastructure services."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from ..adapters import AdapterRegistry, PrepareRequest
from ..adapters.types import AgentInstanceSnapshot, StageSnapshot, TaskCard
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..domain.service import TaskCommandService
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.store import FileStateStore
from .application_delivery import ApplicationDeliveryMixin
from .application_planning import ApplicationPlanningMixin
from .approvals import ApprovalInboxService
from .assets import AssetService
from .instance_runtime_settings import InstanceRuntimeSettingsService
from .start_operations import StartOperationRunner
from .supervisor import ProcessSupervisor
from .task_config import TaskConfigService

CrashHook = Callable[[str], None]


class HarnessApplicationService(ApplicationDeliveryMixin, ApplicationPlanningMixin):
    """Own multi-service workflows so API and Master calls cannot reorder them."""

    def __init__(
        self,
        store: FileStateStore,
        commands: TaskCommandService,
        assets: AssetService,
        approvals: ApprovalInboxService,
        supervisor: ProcessSupervisor,
        adapters: AdapterRegistry,
        task_config: TaskConfigService,
        runtime_settings: InstanceRuntimeSettingsService,
    ) -> None:
        self.store = store
        self.commands = commands
        self.assets = assets
        self.approvals = approvals
        self.supervisor = supervisor
        self.adapters = adapters
        self.task_config = task_config
        self.runtime_settings = runtime_settings
        self.intent_root = store.layout.control_root / "application-intents"
        self.intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.start_operation_runner = StartOperationRunner(self._run_pending_starts)

    @property
    def start_operation_runner_alive(self) -> bool:
        return self.start_operation_runner.alive

    def start_monitoring(self) -> None:
        self.start_operation_runner.start()
        self.start_operation_runner.notify()

    def close_monitoring(self) -> None:
        self.start_operation_runner.close()

    def save_plan_and_create_instances(
        self,
        task_id: str,
        *,
        stages: list[StageSnapshot],
        instances: list[AgentInstanceSnapshot],
        task_cards: list[TaskCard],
        operation_id: str,
        envelope: CommandEnvelope,
        mode: str = "replace",
        expected_plan_revision: int | None = None,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(operation_id, "operation_id")
        request = {
            "task_id": task_id,
            "mode": mode,
            "stages": deepcopy(stages),
            "instances": deepcopy(instances),
            "task_cards": deepcopy(task_cards),
            "envelope": envelope.model_dump(mode="json"),
        }
        if expected_plan_revision is not None:
            request["expected_plan_revision"] = expected_plan_revision
        request_sha256 = digest_json(request)
        intent_path = self._intent_path(operation_id)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            if intent_path.exists():
                intent = read_json(intent_path)
                if intent.get("request_sha256") != request_sha256:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The application operation id was reused for another request.",
                        {"operation_id": operation_id},
                    )
                if intent["state"] == "COMMITTED":
                    return deepcopy(intent["result"])
                if intent["state"] == "ABORTED":
                    self._raise_terminal_intent(intent)
            else:
                self._prevalidate_plan(request)
                prepared_at = utc_now()
                intent = {
                    "schema_version": "1.0",
                    "kind": "SAVE_PLAN_AND_CREATE_INSTANCES",
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "state": "PREPARED",
                    "prepared_at": prepared_at,
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_application_intent")
            return self._resume_save_plan(intent_path, crash_hook)

    def recover(self, *, defer_start_operations: bool = False) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.intent_root.glob("*.json")):
            operation_id = path.stem
            initial = read_json(path)
            task_id = initial.get("task_id") or initial.get("request", {}).get("task_id")
            if not isinstance(task_id, str):
                raise HarnessError("VALIDATION_ERROR", "The application intent has no task owner.")
            with (
                FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
                FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
            ):
                intent = read_json(path)
                if intent["state"] in {"COMMITTED", "ABORTED"}:
                    continue
                try:
                    if intent["kind"] == "SAVE_PLAN_AND_CREATE_INSTANCES":
                        with self.commands.task_guard(task_id):
                            result = self._resume_save_plan(path, None)
                    elif intent["kind"] == "START_READY_INSTANCES":
                        self._migrate_start_intent(path, intent)
                        if defer_start_operations:
                            results.append(
                                {
                                    "operation_id": operation_id,
                                    "status": "PENDING",
                                    "result": self.get_start_operation(operation_id),
                                }
                            )
                            continue
                        result = self._resume_start(path, None)
                    elif intent["kind"] in {"START_INSTANCE", "RESTART_INSTANCE"}:
                        result = self._resume_instance_operation(path)
                    elif intent["kind"] == "CANCEL_TASK":
                        result = self._resume_cancel_task(path, None)
                    elif intent["kind"] == "RESOLVE_APPROVAL":
                        result = self._resume_approval(path, None)
                    else:
                        raise HarnessError(
                            "VALIDATION_ERROR", "The application intent kind is invalid."
                        )
                    results.append(
                        {"operation_id": operation_id, "status": "RECOVERED", "result": result}
                    )
                except HarnessError as exc:
                    state = read_json(path)["state"]
                    results.append(
                        {
                            "operation_id": operation_id,
                            "status": "ABORTED" if state == "ABORTED" else "PENDING",
                            "error_code": exc.code,
                        }
                    )
        self.approvals.reconcile_terminal_notifications()
        return results

    def confirm_and_start_ready_instances(
        self,
        task_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        request = {
            "task_id": task_id,
            "envelope": envelope.model_dump(mode="json"),
        }
        request_sha256 = digest_json(request)
        intent_path = self._intent_path(operation_id)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
        ):
            if intent_path.exists():
                intent = read_json(intent_path)
                if (
                    intent.get("kind") != "START_READY_INSTANCES"
                    or intent.get("request_sha256") != request_sha256
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The application operation id was reused for another request.",
                        {"operation_id": operation_id},
                    )
                if intent["state"] == "COMMITTED":
                    return self._start_operation_summary(intent)
                if intent["state"] == "ABORTED":
                    self._raise_terminal_intent(intent)
            else:
                plan = self._plan(task_id)
                if plan["task"]["status"] not in {
                    "AWAITING_START_CONFIRMATION",
                    "RUNNING",
                    "BLOCKED_UNAVAILABLE",
                }:
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION",
                        "Only a planned task may start ready Agent instances.",
                        {"current": plan["task"]["status"]},
                    )
                ready_stage_ids = {
                    item["stage_id"]
                    for item in plan["stages"]
                    if item["status"] == "READY"
                }
                targets = [
                    item["instance_id"]
                    for item in plan["instances"]
                    if item["status"] == "READY" and item["stage_id"] in ready_stage_ids
                ]
                unavailable = [
                    item["instance_id"]
                    for item in plan["instances"]
                    if item["status"] == "UNAVAILABLE"
                ]
                intent = {
                    "schema_version": "1.1",
                    "kind": "START_READY_INSTANCES",
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "target_instance_ids": targets,
                    "unavailable": unavailable,
                    "instance_progress": {
                        instance_id: {
                            "state": "PENDING",
                            "attempt": 0,
                            "launch_id": None,
                            "side_effect_stage": "NONE",
                            "last_error": None,
                            "updated_at": utc_now(),
                        }
                        for instance_id in targets
                    },
                    "state": "QUEUED",
                    "last_error": None,
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "completed_at": None,
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
            self.task_config.lock_for_start(task_id)
            if crash_hook:
                crash_hook("after_start_intent")
            self._confirm_start_intent(intent_path)
            if self.start_operation_runner.alive and crash_hook is None:
                self.start_operation_runner.notify()
                return self.get_start_operation(operation_id)
            return self._resume_start(intent_path, crash_hook)

    def latest_start_operation(
        self, task_id: str, *, instance_id: str | None = None
    ) -> dict[str, Any] | None:
        validate_identifier(task_id, "task_id")
        candidates: list[dict[str, Any]] = []
        for path in self.intent_root.glob("*.json"):
            intent = read_json(path)
            if (
                intent.get("kind") != "START_READY_INSTANCES"
                or intent.get("request", {}).get("task_id") != task_id
                or (
                    instance_id is not None
                    and instance_id not in intent.get("target_instance_ids", [])
                )
            ):
                continue
            candidates.append(intent)
        if not candidates:
            return None
        latest = max(
            candidates,
            key=lambda item: str(item.get("updated_at") or item.get("prepared_at") or ""),
        )
        latest = self._migrate_start_intent(
            self._intent_path(latest["operation_id"]), latest, persist=False
        )
        return self._start_operation_summary(latest)

    def get_start_operation(self, operation_id: str) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        path = self._intent_path(operation_id)
        if not path.is_file():
            raise HarnessError("TASK_NOT_FOUND", "The requested start operation does not exist.")
        intent = read_json(path)
        if intent.get("kind") != "START_READY_INSTANCES":
            raise HarnessError("TASK_NOT_FOUND", "The requested start operation does not exist.")
        intent = self._migrate_start_intent(path, intent, persist=False)
        return self._start_operation_summary(intent)

    def retry_start_operation(
        self, operation_id: str, *, envelope: CommandEnvelope
    ) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        path = self._intent_path(operation_id)
        with FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds):
            if not path.is_file():
                raise HarnessError(
                    "TASK_NOT_FOUND", "The requested start operation does not exist."
                )
            intent = self._migrate_start_intent(path, read_json(path))
            if envelope.actor_type not in {"human", "master"}:
                raise HarnessError(
                    "VALIDATION_ERROR", "Only a human or Master may recover a start."
                )
            retry_request_sha256 = digest_json(envelope.model_dump(mode="json"))
            if intent.get("last_retry_idempotency_key") == envelope.idempotency_key:
                if intent.get("last_retry_request_sha256") != retry_request_sha256:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The start recovery idempotency key was reused for another request.",
                    )
                return self._start_operation_summary(intent)
            if intent["state"] != "RETRYABLE_FAILED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "Only a retryable failed start operation may be recovered.",
                    {"current": intent["state"]},
                )
            task_id = intent["request"]["task_id"]
            self.commands.validate_task_revision(task_id, envelope.expected_revision)
            now = utc_now()
            for progress in intent["instance_progress"].values():
                if progress["state"] == "RETRYABLE_FAILED":
                    progress.update(
                        {
                            "state": "PENDING",
                            "attempt": int(progress.get("attempt", 0)) + 1,
                            "last_error": None,
                            "updated_at": now,
                        }
                    )
            intent.update(
                {
                    "state": "QUEUED",
                    "last_error": None,
                    "last_retry_idempotency_key": envelope.idempotency_key,
                    "last_retry_request_sha256": retry_request_sha256,
                    "updated_at": now,
                }
            )
            atomic_write_json(path, intent)
        if self.start_operation_runner.alive:
            self.start_operation_runner.notify()
            return self._start_operation_summary(intent)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
        ):
            return self._resume_start(path, None)

    def _run_pending_starts(self) -> None:
        for path in sorted(self.intent_root.glob("*.json")):
            intent = read_json(path)
            if intent.get("kind") != "START_READY_INSTANCES":
                continue
            intent = self._migrate_start_intent(path, intent)
            if intent["state"] not in {"QUEUED", "RUNNING"}:
                continue
            task_id = intent["request"]["task_id"]
            with (
                FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
                FileLock(
                    self._intent_lock(intent["operation_id"]),
                    self.store.lock_timeout_seconds,
                ),
            ):
                latest = read_json(path)
                if latest["state"] in {"QUEUED", "RUNNING"}:
                    self._resume_start(path, None)

    def cancel_instance(
        self,
        task_id: str,
        instance_id: str,
        *,
        operation_id: str | None = None,
        envelope: CommandEnvelope | None = None,
    ) -> dict[str, Any]:
        if envelope is not None:
            if envelope.actor_type not in {"human", "master"}:
                raise HarnessError(
                    "VALIDATION_ERROR", "Only a human or Master may cancel an instance."
                )
            with FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds):
                replayed = self._replayed_instance_transition(
                    task_id, instance_id, "CANCELLED", envelope.idempotency_key
                )
                if replayed is not None:
                    return replayed
                self.commands.validate_task_revision(task_id, envelope.expected_revision)
                return self._cancel_instance(
                    task_id, instance_id, operation_id=operation_id, envelope=envelope
                )
        return self._cancel_instance(task_id, instance_id, operation_id=operation_id)

    def _cancel_instance(
        self,
        task_id: str,
        instance_id: str,
        *,
        operation_id: str | None,
        envelope: CommandEnvelope | None = None,
    ) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        if instance["status"] == "CANCELLED":
            return instance
        stop_operation = operation_id or self._derived_id("cancel", task_id, instance_id)
        validate_identifier(stop_operation, "operation_id")
        if instance["status"] in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
            adapter = self.adapters.get(instance["agent_type"])
            # The process group is still the authoritative cancellation
            # boundary when the Agent endpoint is already unavailable.
            with suppress(HarnessError):
                adapter.stop(
                    instance_id,
                    "harness_instance_cancelled",
                    self._derived_id("stop", stop_operation, instance_id),
                )
        return self.supervisor.cancel_instance(
            task_id,
            instance_id,
            idempotency_key=None if envelope is None else envelope.idempotency_key,
        )

    def cancel_task(
        self,
        task_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        """Durably stop every frozen child before committing task cancellation."""

        validate_identifier(operation_id, "operation_id")
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError("VALIDATION_ERROR", "Only a human or Master may cancel a task.")
        command_request = {"task_id": task_id}
        request = {
            "task_id": task_id,
            "envelope": envelope.model_dump(mode="json"),
        }
        request_sha256 = digest_json(request)
        intent_path = self._intent_path(operation_id)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
        ):
            if intent_path.exists():
                intent = read_json(intent_path)
                if (
                    intent.get("kind") != "CANCEL_TASK"
                    or intent.get("request_sha256") != request_sha256
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The application operation id was reused for another request.",
                        {"operation_id": operation_id},
                    )
                if intent["state"] == "COMMITTED":
                    return deepcopy(intent["result"])
                if intent["state"] == "ABORTED":
                    self._raise_terminal_intent(intent)
                return self._resume_cancel_task(intent_path, crash_hook)
            replayed = self.store.idempotency.lookup(
                task_id,
                envelope.idempotency_key,
                "cancel_task",
                command_request,
            )
            if replayed is not None:
                return replayed
            self.commands.validate_task_revision(task_id, envelope.expected_revision)
            plan = self.store.plan.get(task_id, task_id)
            transitions = self.commands.machine.catalog["agent_instance"]["transitions"]
            targets = [
                {
                    "instance_id": instance["instance_id"],
                    "initial_status": instance["status"],
                }
                for instance in ([] if plan is None else plan["instances"])
                if "CANCELLED" in transitions[instance["status"]]
            ]
            intent = {
                "schema_version": "1.0",
                "kind": "CANCEL_TASK",
                "operation_id": operation_id,
                "request_sha256": request_sha256,
                "request": request,
                "target_instances": targets,
                "instance_progress": {
                    item["instance_id"]: {"state": "PENDING"} for item in targets
                },
                "state": "PREPARED",
                "prepared_at": utc_now(),
                "result": None,
            }
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook("after_cancel_task_intent")
            return self._resume_cancel_task(intent_path, crash_hook)

    def _resume_cancel_task(
        self, intent_path: Path, crash_hook: CrashHook | None
    ) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        envelope = CommandEnvelope.model_validate(request["envelope"])
        task_id = request["task_id"]
        operation_id = intent["operation_id"]
        transitions = self.commands.machine.catalog["agent_instance"]["transitions"]
        for target in intent["target_instances"]:
            instance_id = target["instance_id"]
            progress = intent["instance_progress"][instance_id]
            if progress["state"] == "CANCELLED":
                continue
            progress.update(
                {
                    "state": "CANCELLING",
                    "started_at": progress.get("started_at") or utc_now(),
                }
            )
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook(f"before_instance_cancel:{instance_id}")
            instance = self._instance(task_id, instance_id)
            if instance["status"] != "CANCELLED":
                if "CANCELLED" not in transitions[instance["status"]]:
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION",
                        "A frozen task-cancellation target can no longer be cancelled.",
                        {"instance_id": instance_id, "current": instance["status"]},
                    )
                self._cancel_instance(
                    task_id,
                    instance_id,
                    operation_id=self._derived_id("cancel", operation_id, instance_id),
                )
            if crash_hook:
                crash_hook(f"after_instance_cancel_effect:{instance_id}")
            progress.update({"state": "CANCELLED", "completed_at": utc_now()})
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook(f"after_instance_cancel:{instance_id}")
        if crash_hook:
            crash_hook("before_task_cancel_commit")
        current_revision = self.store.task.revision(task_id, task_id)
        result = self.commands.cancel_task(
            task_id,
            CommandEnvelope(
                idempotency_key=envelope.idempotency_key,
                actor_type=envelope.actor_type,
                actor_id=envelope.actor_id,
                expected_revision=current_revision,
            ),
        )
        if crash_hook:
            crash_hook("after_task_cancel_commit")
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        if crash_hook:
            crash_hook("after_cancel_task_intent_commit")
        return deepcopy(result)

    def start_instance(
        self,
        task_id: str,
        instance_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        """Start one ready instance after the task-level start policy is active."""

        return self._prepare_instance_operation(
            "START_INSTANCE",
            task_id,
            instance_id,
            operation_id=operation_id,
            envelope=envelope,
        )

    def restart_instance(
        self,
        task_id: str,
        instance_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        """Restart a pinned runtime while preserving an already accepted Agent job."""

        return self._prepare_instance_operation(
            "RESTART_INSTANCE",
            task_id,
            instance_id,
            operation_id=operation_id,
            envelope=envelope,
        )

    def _prepare_instance_operation(
        self,
        kind: str,
        task_id: str,
        instance_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human or Master may control an instance."
            )
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "envelope": envelope.model_dump(mode="json"),
        }
        request_sha256 = digest_json(request)
        intent_path = self._intent_path(operation_id)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
        ):
            if intent_path.exists():
                intent = read_json(intent_path)
                if intent.get("kind") != kind or intent.get("request_sha256") != request_sha256:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The application operation id was reused for another request.",
                        {"operation_id": operation_id},
                    )
                if intent["state"] == "COMMITTED":
                    return deepcopy(intent["result"])
                if intent["state"] == "ABORTED":
                    self._raise_terminal_intent(intent)
            else:
                self.commands.validate_task_revision(task_id, envelope.expected_revision)
                plan = self._plan(task_id)
                instance = self._instance(task_id, instance_id)
                if kind == "START_INSTANCE":
                    if plan["task"]["status"] == "AWAITING_START_CONFIRMATION":
                        raise HarnessError(
                            "INVALID_STATE_TRANSITION",
                            "Confirm the task start before starting an individual instance.",
                        )
                    allowed = {"READY"}
                else:
                    allowed = {
                        "RUNNING",
                        "WAITING_APPROVAL",
                        "FAILED_TO_START",
                        "FAILED",
                        "CRASHED",
                    }
                if instance["status"] not in allowed:
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION",
                        "This instance state cannot execute the requested operation.",
                        {"current": instance["status"], "operation": kind},
                    )
                if kind == "START_INSTANCE":
                    self._require_ppt_start_gate(task_id, instance_id)
                intent = {
                    "schema_version": "1.0",
                    "kind": kind,
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if kind == "START_INSTANCE":
                    self.task_config.lock_for_start(task_id)
            return self._resume_instance_operation(intent_path)

    def _resume_instance_operation(self, intent_path: Path) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        task_id = request["task_id"]
        instance_id = request["instance_id"]
        operation_id = intent["operation_id"]
        if intent["kind"] == "START_INSTANCE":
            self.task_config.lock_for_start(task_id)
        adapter, spec = self._prepare_instance(task_id, instance_id)
        launch_prefix = "launch" if intent["kind"] == "START_INSTANCE" else "restart"
        launch_id = self._derived_id(launch_prefix, operation_id, instance_id)
        attempt_id = self._derived_id("attempt", operation_id, instance_id)
        if intent["kind"] == "START_INSTANCE":
            launch = self.supervisor.start_instance(
                task_id,
                instance_id,
                spec,
                launch_id=launch_id,
                attempt_id=attempt_id,
            )
            recovery = adapter.recover(
                cast(AgentInstanceSnapshot, self._instance(task_id, instance_id))
            )
        else:
            launch = self.supervisor.restart_instance(
                task_id,
                instance_id,
                spec,
                launch_id=launch_id,
                attempt_id=attempt_id,
            )
            recovery = adapter.recover(
                cast(AgentInstanceSnapshot, self._instance(task_id, instance_id))
            )
        adapter_result: dict[str, Any]
        if recovery.recovered:
            adapter_result = {
                "accepted": True,
                "operation_id": attempt_id,
                "details": {"mode": "recovered", **deepcopy(recovery.details)},
            }
        else:
            started = adapter.start(instance_id, attempt_id)
            if not started.accepted:
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The Agent adapter rejected the restart operation.",
                )
            adapter_result = {
                "accepted": True,
                "operation_id": started.operation_id,
                "details": {"mode": "started", **deepcopy(started.details)},
            }
        result = {
            "instance": self._instance(task_id, instance_id),
            "launch": self._launch_summary(launch),
            "adapter": adapter_result,
        }
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return deepcopy(result)

    def archive_instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        return self._archive_instance(task_id, instance_id)

    def retry_rejected_delivery(
        self,
        task_id: str,
        instance_id: str,
        *,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only a human or Master may retry a rejected delivery.",
            )
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            transition = self._retry_rejected_delivery_locked(task_id, instance_id, envelope)
        observed = self.observe_instance(task_id, instance_id)
        return {"reopened": transition, "result": observed}

    def _retry_rejected_delivery_locked(
        self,
        task_id: str,
        instance_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        ready_request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "target_status": "READY",
        }
        ready_replay = self.store.idempotency.lookup(
            task_id,
            envelope.idempotency_key,
            "transition_instance",
            ready_request,
        )
        instance = self._instance(task_id, instance_id)
        if ready_replay is None:
            self.commands.validate_task_revision(task_id, envelope.expected_revision)
            if (
                instance["status"] not in {"FAILED", "READY", "STARTING", "RUNNING"}
                or not isinstance(instance.get("delivery_rejection"), dict)
            ):
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "Only an instance with a rejected delivery may retry publication.",
                )

        transition = ready_replay
        if instance["status"] == "FAILED":
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                "READY",
                envelope,
            )
            instance = self._instance(task_id, instance_id)
        if instance["status"] == "READY":
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                "STARTING",
                self._delivery_retry_envelope(envelope, "starting", task_id),
            )
            instance = self._instance(task_id, instance_id)
        if instance["status"] == "STARTING":
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                "RUNNING",
                self._delivery_retry_envelope(envelope, "running", task_id),
            )
        if transition is None:
            transition = {"instance": deepcopy(instance)}
        return transition

    def _delivery_retry_envelope(
        self,
        root: CommandEnvelope,
        step: str,
        task_id: str,
    ) -> CommandEnvelope:
        return CommandEnvelope(
            idempotency_key=(
                f"delivery-retry-{digest_json({'root': root.idempotency_key, 'step': step})[:32]}"
            ),
            actor_type=root.actor_type,
            actor_id=root.actor_id,
            expected_revision=self.store.task.revision(task_id, task_id),
        )

    def archive_instance_command(
        self,
        task_id: str,
        instance_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        if envelope.actor_type != "human":
            raise HarnessError("VALIDATION_ERROR", "Only a human may archive an instance.")
        with FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds):
            replayed = self._replayed_instance_transition(
                task_id, instance_id, "ARCHIVED", envelope.idempotency_key
            )
            if replayed is not None:
                return replayed
            self.commands.validate_task_revision(task_id, envelope.expected_revision)
            return self._archive_instance(
                task_id, instance_id, idempotency_key=envelope.idempotency_key
            )

    def _archive_instance(
        self, task_id: str, instance_id: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        if instance["status"] == "ARCHIVED":
            return instance
        return self.supervisor.archive_instance(
            task_id, instance_id, idempotency_key=idempotency_key
        )

    def _replayed_instance_transition(
        self, task_id: str, instance_id: str, target_status: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        result = self.store.idempotency.lookup(
            task_id,
            idempotency_key,
            "transition_instance",
            {
                "task_id": task_id,
                "instance_id": instance_id,
                "target_status": target_status,
            },
        )
        if result is None:
            return None
        return next(
            deepcopy(item)
            for item in result["plan"]["instances"]
            if item["instance_id"] == instance_id
        )

    def _prepare_instance(self, task_id: str, instance_id: str):
        plan = self._plan(task_id)
        instance = next(
            (item for item in plan["instances"] if item["instance_id"] == instance_id),
            None,
        )
        card = next(
            (item for item in plan["task_cards"] if item["instance_id"] == instance_id),
            None,
        )
        if instance is None or card is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        adapter = self.adapters.get(instance["agent_type"])
        if not adapter.available:
            raise HarnessError("ADAPTER_UNAVAILABLE", "The instance adapter is not available.")
        if instance["agent_type"] == "image":
            self.runtime_settings.ensure_before_start_locked(task_id, instance_id)
        self._require_valid_card(adapter, card)
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        return adapter, adapter.prepare(
            PrepareRequest(
                instance=deepcopy(instance),
                task_card=deepcopy(card),
                task_root=task_root,
                config_ref=task_root / "instances" / instance_id / "runtime" / "runtime.yaml",
            )
        )

    def _require_ppt_start_gate(self, task_id: str, instance_id: str) -> None:
        """Require every Image branch to be manually finished before PPT starts."""

        plan = self._plan(task_id)
        target = next(
            (item for item in plan["instances"] if item["instance_id"] == instance_id),
            None,
        )
        if target is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        if target["agent_type"] != "ppt":
            return
        unfinished = [
            item["instance_id"]
            for item in plan["instances"]
            if item["agent_type"] == "image" and not item.get("manual_finished", False)
        ]
        if unfinished:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "All Image instances must be manually marked finished before PPT can start.",
                {
                    "instance_id": instance_id,
                    "unfinished_instance_ids": unfinished,
                },
            )



    def _plan(self, task_id: str) -> dict[str, Any]:
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            raise HarnessError("TASK_NOT_FOUND", "The task does not have a saved plan.")
        return deepcopy(plan)

    def _instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        return deepcopy(instance)

    def _intent_path(self, operation_id: str) -> Path:
        return self.intent_root / f"{operation_id}.json"

    def _intent_lock(self, operation_id: str) -> Path:
        return self.store.layout.control_root / "locks" / f"application-{operation_id}.lock"

    def _task_lock(self, task_id: str) -> Path:
        return self.store.layout.control_root / "locks" / f"application-task-{task_id}.lock"

    @staticmethod
    def _derived_id(prefix: str, operation_id: str, instance_id: str) -> str:
        digest = hashlib.sha256(f"{operation_id}:{instance_id}".encode()).hexdigest()
        return f"{prefix}_{digest[:24]}"

    @staticmethod
    def _launch_summary(launch: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "task_id",
            "instance_id",
            "launch_id",
            "attempt_id",
            "state",
            "host",
            "port",
            "pid",
            "started_at",
            "code_identity",
        )
        return {name: launch.get(name) for name in fields}
