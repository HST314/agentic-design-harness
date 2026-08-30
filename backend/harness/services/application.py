"""Recoverable application use cases above domain and infrastructure services."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Collection
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..adapters import AdapterRegistry, PrepareRequest
from ..adapters.types import AgentInstanceSnapshot, StageSnapshot, TaskCard
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..domain.service import TaskCommandService
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
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

_ARCHIVE_TARGET_STATUSES = frozenset(
    {"STARTING", "RUNNING", "WAITING_APPROVAL", "SUCCEEDED", "FAILED"}
)
_ARCHIVE_DRAIN_POLL_SECONDS = 2.0
_DEFAULT_ARCHIVE_DRAIN_TIMEOUT_SECONDS = 60.0


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
        self._instance_start_reconciled = False
        self.start_operation_runner = StartOperationRunner(self._run_pending_starts)
        self.observation_runner = StartOperationRunner(
            self.observe_active_instances,
            interval_seconds=2.0,
            thread_name="harness-agent-observations",
        )
        self._observation_logger = logging.getLogger("harness.agent_observations")

    @property
    def start_operation_runner_alive(self) -> bool:
        return self.start_operation_runner.alive

    @property
    def observation_runner_alive(self) -> bool:
        return self.observation_runner.alive

    def start_monitoring(self) -> None:
        self.start_operation_runner.start()
        self.start_operation_runner.notify()
        self.observation_runner.start()
        self.observation_runner.notify()

    def close_monitoring(self) -> None:
        self.observation_runner.close()
        self.start_operation_runner.close()

    def observe_active_instances(self) -> None:
        """Reconcile active Image/PPT instances independently of browser navigation."""

        tasks_root = self.store.layout.control_root / "tasks"
        for task_directory in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
            if not task_directory.is_dir():
                continue
            task_id = task_directory.name
            plan = self.store.plan.get(task_id, task_id)
            if plan is None:
                continue
            for instance in plan["instances"]:
                if (
                    instance["agent_type"] not in {"image", "ppt"}
                    or instance["status"]
                    not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}
                ):
                    continue
                adapter = self.adapters.get_optional(instance["agent_type"])
                if adapter is None or not adapter.available:
                    continue
                try:
                    self.observe_instance(task_id, instance["instance_id"])
                except HarnessError as exc:
                    self._observation_logger.warning(
                        "agent_observation_failed",
                        extra={
                            "fields": {
                                "task_id": task_id,
                                "instance_id": instance["instance_id"],
                                "error_code": exc.code,
                                "error_message": exc.message,
                            }
                        },
                    )
                except Exception as exc:
                    self._observation_logger.exception(
                        "agent_observation_failed",
                        extra={
                            "fields": {
                                "task_id": task_id,
                                "instance_id": instance["instance_id"],
                                "error_type": type(exc).__name__,
                            }
                        },
                    )

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
        self._require_task_not_archived(task_id)
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
                if intent["state"] in {"COMMITTED", "ABORTED", "SUPERSEDED"}:
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
                        intent = self._migrate_instance_start_intent(path, intent)
                        results.append(
                            {
                                "operation_id": operation_id,
                                "status": "PENDING",
                                "result": self._start_operation_summary(intent),
                            }
                        )
                        continue
                    elif intent["kind"] == "CANCEL_TASK":
                        result = self._resume_cancel_task(path, None)
                    elif intent["kind"] == "ARCHIVE_TASK":
                        result = self._resume_archive_task(path, None)
                    elif intent["kind"] == "RESUME_TASK":
                        result = self._resume_resume_task(path, None)
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
        only_instance_ids: Collection[str] | None = None,
    ) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        self._require_task_not_archived(task_id)
        only = None if only_instance_ids is None else sorted(set(only_instance_ids))
        request = {
            "task_id": task_id,
            "envelope": envelope.model_dump(mode="json"),
            "only_instance_ids": only,
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
                startable_stage_ids = {
                    item["stage_id"]
                    for item in plan["stages"]
                    if item["status"] in {"READY", "RUNNING"}
                }
                targets = [
                    item["instance_id"]
                    for item in plan["instances"]
                    if item["status"] == "READY"
                    and item["stage_id"] in startable_stage_ids
                ]
                unavailable = [
                    item["instance_id"]
                    for item in plan["instances"]
                    if item["status"] == "UNAVAILABLE"
                ]
                if only is not None:
                    startable = set(targets)
                    rejected = [instance_id for instance_id in only if instance_id not in startable]
                    if rejected:
                        raise HarnessError(
                            "INVALID_STATE_TRANSITION",
                            "Only ready instances of this task may be started.",
                            {"instance_ids": rejected},
                        )
                    targets = [instance_id for instance_id in targets if instance_id in set(only)]
                    if not targets:
                        raise HarnessError(
                            "VALIDATION_ERROR",
                            "At least one ready instance must be selected to start.",
                        )
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
            kind = intent.get("kind")
            target_ids = (
                intent.get("target_instance_ids", [])
                if kind == "START_READY_INSTANCES"
                else [intent.get("request", {}).get("instance_id")]
            )
            if (
                kind not in {"START_READY_INSTANCES", "START_INSTANCE", "RESTART_INSTANCE"}
                or intent.get("request", {}).get("task_id") != task_id
                or intent.get("state") == "SUPERSEDED"
                or (instance_id is None and kind != "START_READY_INSTANCES")
                or (
                    instance_id is not None
                    and instance_id not in target_ids
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
        latest = self._migrate_operation_intent(
            self._intent_path(latest["operation_id"]), latest, persist=False
        )
        return self._start_operation_summary(latest)

    def get_start_operation(self, operation_id: str) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        path = self._intent_path(operation_id)
        if not path.is_file():
            raise HarnessError("TASK_NOT_FOUND", "The requested start operation does not exist.")
        intent = read_json(path)
        if intent.get("kind") not in {
            "START_READY_INSTANCES",
            "START_INSTANCE",
            "RESTART_INSTANCE",
        }:
            raise HarnessError("TASK_NOT_FOUND", "The requested start operation does not exist.")
        intent = self._migrate_operation_intent(path, intent, persist=False)
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
            intent = self._migrate_operation_intent(path, read_json(path))
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
            recoverable_ppt_gate_failure = self._is_recoverable_ppt_gate_failure(intent)
            if intent["state"] != "RETRYABLE_FAILED" and not recoverable_ppt_gate_failure:
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "Only a retryable failed start operation may be recovered.",
                    {"current": intent["state"]},
                )
            max_attempts = int(intent.get("max_attempts", 0))
            attempts_exhausted = bool(
                max_attempts
                and intent["instance_progress"]
                and all(
                    int(progress.get("attempt", 0)) >= max_attempts
                    for progress in intent["instance_progress"].values()
                )
            )
            if (
                not recoverable_ppt_gate_failure
                and not self._start_operation_summary(intent)["retry_allowed"]
            ) or (recoverable_ppt_gate_failure and attempts_exhausted):
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "The start operation exhausted its retry attempts.",
                    {"operation_id": operation_id},
                )
            task_id = intent["request"]["task_id"]
            self.commands.validate_task_revision(task_id, envelope.expected_revision)
            now = utc_now()
            if recoverable_ppt_gate_failure:
                for progress in intent["instance_progress"].values():
                    if progress["state"] == "ABORTED":
                        progress["state"] = "RETRYABLE_FAILED"
                        if isinstance(progress.get("last_error"), dict):
                            progress["last_error"]["retryable"] = True
                intent["state"] = "RETRYABLE_FAILED"
                if isinstance(intent.get("last_error"), dict):
                    intent["last_error"]["retryable"] = True
                intent["completed_at"] = None
                intent.pop("error", None)
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
        if intent["kind"] in {"START_INSTANCE", "RESTART_INSTANCE"}:
            return self._start_operation_summary(intent)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
        ):
            return self._resume_start(path, None)

    def _run_pending_starts(self) -> None:
        if not self._instance_start_reconciled:
            self._supersede_stale_instance_operations()
            self._instance_start_reconciled = True
        for path in sorted(self.intent_root.glob("*.json")):
            intent = read_json(path)
            kind = intent.get("kind")
            if kind not in {
                "START_READY_INSTANCES",
                "START_INSTANCE",
                "RESTART_INSTANCE",
            }:
                continue
            if kind == "START_READY_INSTANCES":
                intent = self._migrate_start_intent(path, intent)
                resumable_states = {"QUEUED", "RUNNING"}
            else:
                intent = self._migrate_instance_start_intent(path, intent)
                resumable_states = {"QUEUED", "RUNNING"}
            if intent["state"] not in resumable_states:
                continue
            task_id = intent["request"]["task_id"]
            if kind == "START_READY_INSTANCES":
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
                continue
            with (
                FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
                FileLock(
                    self._intent_lock(intent["operation_id"]),
                    self.store.lock_timeout_seconds,
                ),
            ):
                latest = self._migrate_instance_start_intent(path, read_json(path))
                if latest["state"] not in {"QUEUED", "RUNNING"}:
                    continue
                if latest["state"] == "QUEUED":
                    latest.update({"state": "RUNNING", "updated_at": utc_now()})
                    atomic_write_json(path, latest)
            self._resume_instance_operation(path)

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
            self._require_task_not_archived(task_id)
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

    def archive_task(
        self,
        task_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
        drain_timeout_seconds: float | None = None,
        force: bool = False,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        """Drain and park every live child process, then mark the task ARCHIVED.

        Archiving is reversible: the per-instance and pre-archive task statuses
        are snapshotted into the intent so a later resume can restore them.
        """

        validate_identifier(operation_id, "operation_id")
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError("VALIDATION_ERROR", "Only a human or Master may archive a task.")
        drain_timeout = (
            _DEFAULT_ARCHIVE_DRAIN_TIMEOUT_SECONDS
            if drain_timeout_seconds is None
            else float(drain_timeout_seconds)
        )
        if not 0 <= drain_timeout <= 3600:
            raise HarnessError("VALIDATION_ERROR", "The archive drain timeout is invalid.")
        request = {
            "task_id": task_id,
            "envelope": envelope.model_dump(mode="json"),
            "drain_timeout_seconds": drain_timeout,
            "force": bool(force),
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
                    intent.get("kind") != "ARCHIVE_TASK"
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
                return self._resume_archive_task(intent_path, crash_hook)
            replayed = self.store.idempotency.lookup(
                task_id,
                envelope.idempotency_key,
                "archive_task",
                {"task_id": task_id},
            )
            if replayed is not None:
                return replayed
            self.commands.validate_task_revision(task_id, envelope.expected_revision)
            plan = self.store.plan.get(task_id, task_id)
            task_status = (
                plan["task"]["status"]
                if plan is not None
                else self.commands._task(task_id)["status"]
            )
            if task_status == "ARCHIVED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "The task is already archived.",
                    {"task_id": task_id},
                )
            targets = [
                {
                    "instance_id": instance["instance_id"],
                    "initial_status": instance["status"],
                }
                for instance in ([] if plan is None else plan["instances"])
                if instance["status"] in _ARCHIVE_TARGET_STATUSES
            ]
            intent = {
                "schema_version": "1.0",
                "kind": "ARCHIVE_TASK",
                "operation_id": operation_id,
                "request_sha256": request_sha256,
                "request": request,
                "snapshot": {
                    "pre_archive_status": task_status,
                    "stopped_instance_ids": [item["instance_id"] for item in targets],
                },
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
                crash_hook("after_archive_task_intent")
            return self._resume_archive_task(intent_path, crash_hook)

    def _resume_archive_task(
        self, intent_path: Path, crash_hook: CrashHook | None
    ) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        envelope = CommandEnvelope.model_validate(request["envelope"])
        task_id = request["task_id"]
        operation_id = intent["operation_id"]
        if not request.get("force", False):
            busy = self._drain_archive_targets(
                task_id,
                [item["instance_id"] for item in intent["target_instances"]],
                float(request["drain_timeout_seconds"]),
            )
            if busy:
                error = HarnessError(
                    "ARCHIVE_DRAIN_TIMEOUT",
                    "Agents are still finishing in-flight work; "
                    "retry to keep waiting or archive with force.",
                    {"busy_instance_ids": busy, "operation_id": operation_id},
                )
                intent.update(
                    {
                        "state": "ABORTED",
                        "aborted_at": utc_now(),
                        "error": {
                            "code": error.code,
                            "message": error.message,
                            "details": deepcopy(error.details),
                        },
                    }
                )
                atomic_write_json(intent_path, intent)
                raise error
        for target in intent["target_instances"]:
            instance_id = target["instance_id"]
            progress = intent["instance_progress"][instance_id]
            if progress["state"] == "STOPPED":
                continue
            progress.update(
                {
                    "state": "STOPPING",
                    "started_at": progress.get("started_at") or utc_now(),
                }
            )
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook(f"before_instance_suspend:{instance_id}")
            instance = self._instance(task_id, instance_id)
            if instance["status"] != "STOPPED":
                if instance["status"] in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
                    adapter = self.adapters.get(instance["agent_type"])
                    with suppress(HarnessError):
                        adapter.stop(
                            instance_id,
                            "harness_task_archived",
                            self._derived_id("stop", operation_id, instance_id),
                        )
                self.supervisor.suspend_instance(
                    task_id,
                    instance_id,
                    idempotency_key=f"archive-{operation_id}-{instance_id}",
                )
            if crash_hook:
                crash_hook(f"after_instance_suspend_effect:{instance_id}")
            progress.update({"state": "STOPPED", "completed_at": utc_now()})
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook(f"after_instance_suspend:{instance_id}")
        if crash_hook:
            crash_hook("before_task_archive_commit")
        current_revision = self.store.task.revision(task_id, task_id)
        result = self.commands.archive_task(
            task_id,
            CommandEnvelope(
                idempotency_key=envelope.idempotency_key,
                actor_type=envelope.actor_type,
                actor_id=envelope.actor_id,
                expected_revision=current_revision,
            ),
        )
        if crash_hook:
            crash_hook("after_task_archive_commit")
        self._sync_task_navigation(task_id, archived=True)
        result = {**deepcopy(result), "archive_snapshot": deepcopy(intent["snapshot"])}
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        if crash_hook:
            crash_hook("after_archive_task_intent_commit")
        return deepcopy(result)

    def _drain_archive_targets(
        self, task_id: str, instance_ids: list[str], timeout_seconds: float
    ) -> list[str]:
        """Best-effort wait for in-flight Agent work; returns still-busy instances."""

        deadline = time.monotonic() + timeout_seconds
        busy: list[str] = []
        for instance_id in instance_ids:
            instance = self._instance(task_id, instance_id)
            if instance["status"] not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
                continue
            adapter = self.adapters.get_optional(instance["agent_type"])
            if adapter is None or not adapter.available:
                continue
            while True:
                try:
                    active = adapter.has_active_work(instance_id)
                except Exception:
                    active = False
                if not active:
                    break
                if time.monotonic() >= deadline:
                    busy.append(instance_id)
                    break
                time.sleep(_ARCHIVE_DRAIN_POLL_SECONDS)
        return busy

    def resume_task(
        self,
        task_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        """Restore an archived task and restart the instances its archive stopped."""

        validate_identifier(operation_id, "operation_id")
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError("VALIDATION_ERROR", "Only a human or Master may resume a task.")
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
                    intent.get("kind") != "RESUME_TASK"
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
                return self._resume_resume_task(intent_path, crash_hook)
            self.commands.validate_task_revision(task_id, envelope.expected_revision)
            plan = self.store.plan.get(task_id, task_id)
            task_status = (
                plan["task"]["status"]
                if plan is not None
                else self.commands._task(task_id)["status"]
            )
            if task_status != "ARCHIVED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "Only an archived task may be resumed.",
                    {"task_id": task_id, "current": task_status},
                )
            snapshot = self._latest_archive_snapshot(task_id)
            if snapshot is None:
                snapshot = {
                    "pre_archive_status": "RUNNING" if plan is not None else "DRAFT",
                    "stopped_instance_ids": [
                        instance["instance_id"]
                        for instance in ([] if plan is None else plan["instances"])
                        if instance["status"] == "STOPPED"
                    ],
                }
            intent = {
                "schema_version": "1.0",
                "kind": "RESUME_TASK",
                "operation_id": operation_id,
                "request_sha256": request_sha256,
                "request": request,
                "snapshot": deepcopy(snapshot),
                "instance_progress": {
                    instance_id: {"state": "PENDING"}
                    for instance_id in snapshot["stopped_instance_ids"]
                },
                "task_restored": False,
                "state": "PREPARED",
                "prepared_at": utc_now(),
                "result": None,
            }
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook("after_resume_task_intent")
            return self._resume_resume_task(intent_path, crash_hook)

    def _resume_resume_task(
        self, intent_path: Path, crash_hook: CrashHook | None
    ) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        envelope = CommandEnvelope.model_validate(request["envelope"])
        task_id = request["task_id"]
        operation_id = intent["operation_id"]
        snapshot = intent["snapshot"]
        if not intent.get("task_restored"):
            if crash_hook:
                crash_hook("before_task_restore_commit")
            current_revision = self.store.task.revision(task_id, task_id)
            self.commands.restore_task(
                task_id,
                snapshot["pre_archive_status"],
                CommandEnvelope(
                    idempotency_key=envelope.idempotency_key,
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    expected_revision=current_revision,
                ),
            )
            if crash_hook:
                crash_hook("after_task_restore_commit")
            self._sync_task_navigation(task_id, archived=False)
            intent["task_restored"] = True
            atomic_write_json(intent_path, intent)
        restored: list[str] = []
        failed: list[dict[str, Any]] = []
        for instance_id in snapshot["stopped_instance_ids"]:
            progress = intent["instance_progress"][instance_id]
            if progress["state"] in {"RUNNING", "SKIPPED", "FAILED"}:
                if progress["state"] == "RUNNING":
                    restored.append(instance_id)
                if progress["state"] == "FAILED":
                    failed.append(deepcopy(progress["error"]))
                continue
            progress.update(
                {
                    "state": "RESTARTING",
                    "started_at": progress.get("started_at") or utc_now(),
                }
            )
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook(f"before_instance_resume:{instance_id}")
            instance = self._instance(task_id, instance_id)
            if instance["status"] != "STOPPED":
                progress.update({"state": "SKIPPED", "completed_at": utc_now()})
                atomic_write_json(intent_path, intent)
                continue
            try:
                adapter, spec = self._prepare_instance(task_id, instance_id)
                del adapter
                launch = self.supervisor.restart_instance(
                    task_id,
                    instance_id,
                    spec,
                    launch_id=self._derived_id("launch", operation_id, instance_id),
                    attempt_id=self._derived_id("attempt", operation_id, instance_id),
                )
                if launch["state"] != "RUNNING":
                    raise HarnessError(
                        "PROCESS_START_FAILED",
                        "A resumed instance cannot reuse a non-running launch.",
                        {"instance_id": instance_id, "launch_state": launch["state"]},
                    )
            except HarnessError as exc:
                failure = {
                    "instance_id": instance_id,
                    "code": exc.code,
                    "message": exc.message,
                    "details": deepcopy(exc.details),
                }
                progress.update(
                    {"state": "FAILED", "error": failure, "completed_at": utc_now()}
                )
                atomic_write_json(intent_path, intent)
                failed.append(failure)
                continue
            except Exception as exc:
                failure = {
                    "instance_id": instance_id,
                    "code": "PROCESS_START_FAILED",
                    "message": "The instance restart failed unexpectedly.",
                    "details": {"failure_type": type(exc).__name__, "instance_id": instance_id},
                }
                progress.update(
                    {"state": "FAILED", "error": failure, "completed_at": utc_now()}
                )
                atomic_write_json(intent_path, intent)
                failed.append(failure)
                continue
            if crash_hook:
                crash_hook(f"after_instance_resume_effect:{instance_id}")
            progress.update({"state": "RUNNING", "completed_at": utc_now()})
            atomic_write_json(intent_path, intent)
            restored.append(instance_id)
        task = self.store.task.get(task_id, task_id)
        result = {
            "task": deepcopy(task),
            "restored_status": snapshot["pre_archive_status"],
            "restored_instance_ids": restored,
            "failed_instances": failed,
        }
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        if crash_hook:
            crash_hook("after_resume_task_intent_commit")
        return deepcopy(result)

    def _latest_archive_snapshot(self, task_id: str) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        for path in self.intent_root.glob("*.json"):
            intent = read_json(path)
            if (
                intent.get("kind") == "ARCHIVE_TASK"
                and intent.get("state") == "COMMITTED"
                and intent.get("request", {}).get("task_id") == task_id
                and isinstance(intent.get("snapshot"), dict)
            ):
                candidates.append(intent)
        if not candidates:
            return None
        latest = max(candidates, key=lambda item: str(item.get("committed_at") or ""))
        return deepcopy(latest["snapshot"])

    def _require_task_not_archived(self, task_id: str) -> None:
        task = self.store.task.get(task_id, task_id)
        if task is not None and task.get("status") == "ARCHIVED":
            raise HarnessError(
                "TASK_ARCHIVED",
                "The task is archived and read-only; resume it before issuing changes.",
                {"task_id": task_id},
            )

    def _sync_task_navigation(self, task_id: str, *, archived: bool) -> None:
        """Keep the sidebar archived flag consistent with the task lifecycle."""

        current = self.store.task_navigation.get(task_id, task_id)
        current_revision = self.store.task_navigation.revision(task_id, task_id)
        if current is None:
            task = self.store.task.get(task_id, task_id)
            if task is None:
                return
            current = {
                "schema_version": "1.0",
                "task_id": task_id,
                "pinned_at": None,
                "archived_at": None,
                "display_order": 0,
                "revision": 0,
                "updated_at": task["updated_at"],
            }
        if (current.get("archived_at") is not None) == archived:
            return
        now = utc_now()
        updated = deepcopy(current)
        updated["archived_at"] = now if archived else None
        if archived:
            updated["pinned_at"] = None
        updated["revision"] = current_revision + 1
        updated["updated_at"] = now
        self.store.task_navigation.put(
            task_id,
            task_id,
            updated,
            expected_revision=current_revision,
            actor=Actor("system", "task_archive"),
            command="sync_task_archive_navigation",
            idempotency_key=f"sync-nav-archive-{task_id}-{archived}-{current_revision}",
        )

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
        self._require_task_not_archived(task_id)
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
        self._require_task_not_archived(task_id)
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
            if item["agent_type"] == "image"
            and item["status"] not in {"SUCCEEDED", "SKIPPED", "SUPERSEDED", "ARCHIVED"}
            and not item.get("manual_finished", False)
        ]
        if unfinished:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "All Image WorkItems must be completed before PPT can start.",
                {
                    "instance_id": instance_id,
                    "unfinished_instance_ids": unfinished,
                },
            )

    def _is_recoverable_ppt_gate_failure(self, intent: dict[str, Any]) -> bool:
        """Recognize the side-effect-free gate mismatch emitted by the prior release."""

        error = intent.get("last_error")
        if (
            intent.get("kind") != "START_INSTANCE"
            or intent.get("state") != "ABORTED"
            or not isinstance(error, dict)
            or error.get("code") != "INVALID_STATE_TRANSITION"
            or error.get("message")
            != "The instance stage and task are not authorized to start."
        ):
            return False
        task_id = intent.get("request", {}).get("task_id")
        instance_id = intent.get("request", {}).get("instance_id")
        if not isinstance(task_id, str) or not isinstance(instance_id, str):
            return False
        progress = intent.get("instance_progress", {}).get(instance_id)
        if not isinstance(progress, dict) or progress.get("side_effect_stage") != "NONE":
            return False
        try:
            instance = self._instance(task_id, instance_id)
            if instance["agent_type"] != "ppt" or instance["status"] not in {
                "READY",
                "FAILED_TO_START",
            }:
                return False
            self._require_ppt_start_gate(task_id, instance_id)
        except HarnessError:
            return False
        return True

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
