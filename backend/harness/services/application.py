"""Recoverable application use cases above domain and infrastructure services."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, NoReturn

from ..adapters import AdapterRegistry, PrepareRequest
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..domain.service import TaskCommandService
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .approvals import ApprovalInboxService
from .assets import AssetService
from .configuration import ConfigurationService
from .credentials import CredentialPoolService
from .supervisor import ProcessSupervisor

CrashHook = Callable[[str], None]
_DELIVERY_REJECTION_CODES = {
    "ASSET_CORRUPTED",
    "ASSET_VALIDATION_FAILED",
    "VALIDATION_ERROR",
}


class HarnessApplicationService:
    """Own multi-service workflows so API and Master calls cannot reorder them."""

    def __init__(
        self,
        store: FileStateStore,
        commands: TaskCommandService,
        assets: AssetService,
        approvals: ApprovalInboxService,
        credentials: CredentialPoolService,
        supervisor: ProcessSupervisor,
        adapters: AdapterRegistry,
        configuration: ConfigurationService | None = None,
    ) -> None:
        self.store = store
        self.commands = commands
        self.assets = assets
        self.approvals = approvals
        self.credentials = credentials
        self.supervisor = supervisor
        self.adapters = adapters
        self.configuration = configuration
        self.intent_root = store.layout.control_root / "application-intents"
        self.intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save_plan_and_create_instances(
        self,
        task_id: str,
        *,
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        task_cards: list[dict[str, Any]],
        providers: dict[str, str],
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(operation_id, "operation_id")
        request = {
            "task_id": task_id,
            "stages": deepcopy(stages),
            "instances": deepcopy(instances),
            "task_cards": deepcopy(task_cards),
            "providers": dict(sorted(providers.items())),
            "envelope": envelope.model_dump(mode="json"),
        }
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
                    "creation_instances": {
                        item["instance_id"]: self._creation_summary(task_id, item, prepared_at)
                        for item in request["instances"]
                        if item["instance_id"] in providers
                    },
                    "state": "PREPARED",
                    "prepared_at": prepared_at,
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_application_intent")
            return self._resume_save_plan(intent_path, crash_hook)

    def recover(self) -> list[dict[str, Any]]:
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
                    return deepcopy(intent["result"])
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
                targets = [
                    item["instance_id"] for item in plan["instances"] if item["status"] == "READY"
                ]
                unavailable = [
                    item["instance_id"]
                    for item in plan["instances"]
                    if item["status"] == "UNAVAILABLE"
                ]
                intent = {
                    "schema_version": "1.0",
                    "kind": "START_READY_INSTANCES",
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "target_instance_ids": targets,
                    "unavailable": unavailable,
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_start_intent")
            return self._resume_start(intent_path, crash_hook)

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
            return self._resume_instance_operation(intent_path)

    def _resume_instance_operation(self, intent_path: Path) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        task_id = request["task_id"]
        instance_id = request["instance_id"]
        operation_id = intent["operation_id"]
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
            recovery = adapter.recover(self._instance(task_id, instance_id))
        else:
            launch = self.supervisor.restart_instance(
                task_id,
                instance_id,
                spec,
                launch_id=launch_id,
                attempt_id=attempt_id,
            )
            recovery = adapter.recover(self._instance(task_id, instance_id))
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
        if self.configuration is not None:
            self.configuration.mark_restarted(task_id, instance_id)
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
        self._require_valid_card(adapter, card)
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        return adapter, adapter.prepare(
            PrepareRequest(
                instance=deepcopy(instance),
                task_card=deepcopy(card),
                task_root=task_root,
                config_ref=task_root / "instances" / instance_id / "runtime" / "runtime.yaml",
                credential_ref=(
                    instance["credential_pair_ref"],
                    instance["credential_pair_revision"],
                ),
            )
        )

    def observe_instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        """Poll one Agent and persist its deterministic domain-state projection."""

        instance = self._instance(task_id, instance_id)
        if instance["status"] not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
            return {"instance": instance, "observation": None, "transition": None}
        adapter = self.adapters.get(instance["agent_type"])
        observation = adapter.get_status(instance_id)
        if observation.status not in {"RUNNING", "WAITING_APPROVAL", "FAILED"}:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Agent adapter returned a non-projectable status.",
                {"status": observation.status},
            )
        if observation.details.get("completed") is True:
            try:
                delivery = self._collect_publish_and_complete(
                    task_id, instance_id, adapter, observation
                )
            except HarnessError as exc:
                if exc.code not in _DELIVERY_REJECTION_CODES:
                    raise
                return self._record_delivery_rejection(
                    task_id,
                    instance_id,
                    observation,
                    exc,
                )
            return {
                "instance": deepcopy(delivery["instance"]),
                "observation": {
                    "status": observation.status,
                    "step_id": observation.step_id,
                    "capabilities": list(observation.capabilities),
                    "details": deepcopy(observation.details),
                },
                "transition": delivery["transition"],
                "approval": None,
                "delivery": delivery,
            }
        transition = None
        if observation.status != instance["status"]:
            task_revision = self.store.task.revision(task_id, task_id)
            observation_digest = digest_json(
                {
                    "status": observation.status,
                    "step_id": observation.step_id,
                    "capabilities": list(observation.capabilities),
                    "details": observation.details,
                }
            )
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                observation.status,
                CommandEnvelope(
                    idempotency_key=(
                        f"observe-{instance_id}-{task_revision}-{observation_digest[:20]}"
                    ),
                    actor_type="adapter",
                    actor_id=f"{instance['agent_type']}_adapter",
                    expected_revision=task_revision,
                ),
            )
            instance = next(
                item
                for item in transition["plan"]["instances"]
                if item["instance_id"] == instance_id
            )
        approval = None
        if observation.status == "WAITING_APPROVAL":
            operation_id = str(
                observation.details.get("job_id")
                or digest_json(
                    {
                        "instance_id": instance_id,
                        "step_id": observation.step_id,
                        "details": observation.details,
                    }
                )[:24]
            )
            approval = self.approvals.ensure_workflow_approval(
                task_id,
                instance_id,
                step_id=str(observation.step_id),
                capabilities=list(observation.capabilities),
                context=deepcopy(observation.details.get("approval_context") or {}),
                operation_id=operation_id,
            )
        elif observation.status == "FAILED":
            self.approvals.ensure_notification(
                task_id,
                kind="INSTANCE_FAILED",
                owner="human",
                title="Image Agent 执行失败",
                message=f"实例 {instance_id} 已进入失败状态。请查看实例详情。",
                deep_link=f"instances/{instance_id}",
                dedupe_key=(f"instance-failed:{instance_id}"),
                instance_id=instance_id,
            )
        return {
            "instance": deepcopy(instance),
            "observation": {
                "status": observation.status,
                "step_id": observation.step_id,
                "capabilities": list(observation.capabilities),
                "details": deepcopy(observation.details),
            },
            "transition": transition,
            "approval": approval,
        }

    def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        action: str | None,
        payload: dict[str, Any],
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(approval_id, "approval_id")
        validate_identifier(operation_id, "operation_id")
        initial_approval = self.approvals.get_approval(approval_id)
        task_id = initial_approval["approval"]["task_id"]
        request = {
            "approval_id": approval_id,
            "decision": decision,
            "action": action,
            "payload": deepcopy(payload),
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
                    intent.get("kind") != "RESOLVE_APPROVAL"
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
            else:
                approval_details = self.approvals.get_approval(approval_id)
                approval = approval_details["approval"]
                approval_payload = approval_details["payload"]
                if approval["status"] != "PENDING":
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION", "Only a pending approval may be resolved."
                    )
                if envelope.actor_type != approval["owner"]:
                    raise HarnessError(
                        "VALIDATION_ERROR", "The approval must be resolved by its frozen owner."
                    )
                if envelope.expected_revision != approval_details["approval_revision"]:
                    raise HarnessError(
                        "REVISION_CONFLICT",
                        "The approval revision changed before the decision was prepared.",
                        {
                            "expected_revision": envelope.expected_revision,
                            "actual_revision": approval_details["approval_revision"],
                        },
                    )
                if decision == "APPROVED":
                    if action not in approval_payload["available_actions"]:
                        raise HarnessError(
                            "VALIDATION_ERROR",
                            "The selected action is not available for this approval.",
                        )
                elif decision == "REJECTED":
                    if action is not None or payload:
                        raise HarnessError(
                            "VALIDATION_ERROR", "A rejected approval cannot submit an Agent action."
                        )
                else:
                    raise HarnessError("VALIDATION_ERROR", "The approval decision is invalid.")
                intent = {
                    "schema_version": "1.0",
                    "kind": "RESOLVE_APPROVAL",
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "task_id": task_id,
                    "instance_id": approval["instance_id"],
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                    "advance": None,
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_approval_intent")
            return self._resume_approval(intent_path, crash_hook)

    def publish_delivery_and_complete(
        self,
        task_id: str,
        instance_id: str,
        *,
        source_relative_path: str,
        role: str,
        description: str,
        operation_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            instance = self._instance(task_id, instance_id)
            if instance["status"] == "SUCCEEDED":
                return self._replay_completed_delivery(
                    task_id,
                    instance_id,
                    source_relative_path=source_relative_path,
                    role=role,
                    description=description,
                    operation_id=operation_id,
                    envelope=envelope,
                )
            self._require_delivery_command(task_id, instance_id, envelope)
            batch_id = self._manual_delivery_batch_id(task_id, instance_id)
            self.assets.inspect_delivery(
                task_id,
                instance_id,
                source_relative_path=source_relative_path,
                role=role,
                description=description,
            )
            manifest = self.assets.publish_delivery(
                task_id,
                instance_id,
                source_relative_path=source_relative_path,
                role=role,
                description=description,
                idempotency_key=operation_id,
                batch_id=batch_id,
            )
            prepared = self.assets.list_publication_batch(task_id, instance_id, batch_id)
            declared = self._declared_delivery_candidates(task_id, instance_id, prepared)
            transition = None
            if self._required_delivery_selection(task_id, instance_id, declared) is not None:
                self.assets.commit_publication_batch(
                    task_id,
                    instance_id,
                    batch_id=batch_id,
                    manifests=declared,
                )
                transition = self._complete_instance_if_ready(
                    task_id,
                    instance_id,
                    envelope=envelope,
                    require_deliveries=True,
                    candidate_manifests=declared,
                )
            return {
                "manifest": manifest,
                "complete": transition is not None,
                "transition": transition,
            }

    def _replay_completed_delivery(
        self,
        task_id: str,
        instance_id: str,
        *,
        source_relative_path: str,
        role: str,
        description: str,
        operation_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        self._require_owning_adapter(instance, envelope)
        manifest = self.assets.replay_delivery(
            task_id,
            instance_id,
            source_relative_path=source_relative_path,
            role=role,
            description=description,
            idempotency_key=operation_id,
            batch_id=self._manual_delivery_batch_id(task_id, instance_id),
        )
        if manifest is None:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A succeeded instance only accepts an exact delivery replay.",
                {"instance_id": instance_id},
            )
        transition = self.commands.transition_instance(task_id, instance_id, "SUCCEEDED", envelope)
        self._ensure_success_notifications(task_id, instance_id, transition)
        return {"manifest": manifest, "complete": True, "transition": transition}

    def _resume_approval(self, intent_path: Path, crash_hook: CrashHook | None) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        envelope = CommandEnvelope.model_validate(request["envelope"])
        task_id = intent["task_id"]
        instance_id = intent["instance_id"]
        instance = self._instance(task_id, instance_id)
        if request["decision"] == "APPROVED" and intent["advance"] is None:
            adapter = self.adapters.get(instance["agent_type"])
            advance_payload = deepcopy(request["payload"])
            advance_payload["actor"] = envelope.actor_id
            advance = adapter.request_advance(
                instance_id,
                str(request["action"]),
                advance_payload,
                self._derived_id("advance", intent["operation_id"], request["approval_id"]),
            )
            intent["advance"] = {
                "accepted": advance.accepted,
                "operation_id": advance.operation_id,
                "details": deepcopy(advance.details),
            }
            intent["state"] = "ADVANCE_ACCEPTED" if advance.accepted else "ADVANCE_REJECTED"
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook("after_adapter_advance")
        if request["decision"] == "APPROVED" and not intent["advance"]["accepted"]:
            error = HarnessError(
                "INVALID_STATE_TRANSITION",
                "The Agent adapter rejected the approval advance; no decision was committed.",
                {
                    "approval_id": request["approval_id"],
                    "instance_id": instance_id,
                    "operation_id": intent["advance"]["operation_id"],
                },
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
        target_status = "RUNNING" if request["decision"] == "APPROVED" else "FAILED"
        instance = self._instance(task_id, instance_id)
        if instance["status"] != target_status:
            if instance["status"] != "WAITING_APPROVAL":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "The approval no longer owns a waiting Agent instance.",
                    {"current": instance["status"]},
                )
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                target_status,
                CommandEnvelope(
                    idempotency_key=self._derived_id(
                        "decision", intent["operation_id"], request["approval_id"]
                    ),
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    expected_revision=self.store.task.revision(task_id, task_id),
                ),
            )
            instance = next(
                item
                for item in transition["plan"]["instances"]
                if item["instance_id"] == instance_id
            )
            if crash_hook:
                crash_hook("after_approval_instance_transition")
        resolution = self.approvals.commit_resolution(
            request["approval_id"], request["decision"], envelope
        )
        if crash_hook:
            crash_hook("after_approval_commit")
        actor = Actor(envelope.actor_type, envelope.actor_id)
        self.approvals.handle_approval_notification(
            request["approval_id"],
            actor,
            self._derived_id("handled", intent["operation_id"], request["approval_id"]),
        )
        if request["decision"] == "REJECTED":
            self.approvals.ensure_notification(
                task_id,
                kind="INSTANCE_FAILED",
                owner="human",
                title="工作流决议已拒绝",
                message=f"实例 {instance_id} 因审批拒绝已停止。",
                deep_link=f"instances/{instance_id}",
                dedupe_key=f"instance-failed:{instance_id}",
                instance_id=instance_id,
                approval_id=request["approval_id"],
            )
        if crash_hook:
            crash_hook("after_approval_notification")
        result = {
            "approval": resolution["approval"],
            "approval_revision": resolution["approval_revision"],
            "instance": deepcopy(instance),
            "advance": deepcopy(intent["advance"]),
        }
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return result

    def _collect_publish_and_complete(
        self,
        task_id: str,
        instance_id: str,
        adapter,
        observation,
    ) -> dict[str, Any]:
        deliveries = adapter.collect_deliveries(instance_id)
        if not deliveries:
            raise HarnessError(
                "VALIDATION_ERROR", "A completed Image Agent did not expose a delivery."
            )
        envelope = CommandEnvelope(
            idempotency_key=(
                f"complete-{instance_id}-"
                f"{digest_json(sorted(item['sha256'] for item in deliveries))[:20]}"
            ),
            actor_type="adapter",
            actor_id=f"{self._instance(task_id, instance_id)['agent_type']}_adapter",
            expected_revision=self.store.task.revision(task_id, task_id),
        )
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            self._require_delivery_command(task_id, instance_id, envelope)
            candidates = [
                self.assets.inspect_delivery(
                    task_id,
                    instance_id,
                    source_relative_path=delivery["source_relative_path"],
                    role=delivery["role"],
                    description=delivery["description"],
                    expected_sha256=delivery.get("sha256"),
                    derivation=delivery.get("derivation"),
                )
                for delivery in deliveries
            ]
            self._validate_required_delivery_set(task_id, instance_id, candidates)
            batch_payload = sorted(
                candidates,
                key=lambda item: (
                    item["role"],
                    item["kind"],
                    item["mime_type"],
                    item["sha256"],
                    item["source_relative_path"],
                ),
            )
            batch_id = f"batch_{digest_json(batch_payload)[:24]}"
            manifests = []
            for delivery, candidate in zip(deliveries, candidates, strict=True):
                operation_id = f"collect-{instance_id}-{digest_json(candidate)[:32]}"
                manifests.append(
                    self.assets.publish_delivery(
                        task_id,
                        instance_id,
                        source_relative_path=delivery["source_relative_path"],
                        role=delivery["role"],
                        description=delivery["description"],
                        idempotency_key=operation_id,
                        batch_id=batch_id,
                        derivation=candidate["derivation"],
                    )
                )
            self.assets.commit_publication_batch(
                task_id,
                instance_id,
                batch_id=batch_id,
                manifests=manifests,
            )
            transition = self._complete_instance_if_ready(
                task_id,
                instance_id,
                envelope=envelope,
                require_deliveries=True,
                candidate_manifests=manifests,
            )
            assert transition is not None
        instance = next(
            item for item in transition["plan"]["instances"] if item["instance_id"] == instance_id
        )
        return {
            "instance": instance,
            "manifests": manifests,
            "transition": transition,
            "observation": {
                "step_id": observation.step_id,
                "details": deepcopy(observation.details),
            },
        }

    def _record_delivery_rejection(
        self,
        task_id: str,
        instance_id: str,
        observation,
        error: HarnessError,
    ) -> dict[str, Any]:
        current = self._instance(task_id, instance_id)
        if current["status"] not in {"RUNNING", "WAITING_APPROVAL"}:
            raise error
        rejection = {
            "code": error.code,
            "message": error.message[:1000],
            "details": deepcopy(error.details),
        }
        revision = self.store.task.revision(task_id, task_id)
        transition = self.commands.reject_instance_delivery(
            task_id,
            instance_id,
            rejection,
            CommandEnvelope(
                idempotency_key=f"reject-delivery-{digest_json(rejection)[:32]}",
                actor_type="adapter",
                actor_id=f"{current['agent_type']}_adapter",
                expected_revision=revision,
            ),
        )
        instance = next(
            item for item in transition["plan"]["instances"] if item["instance_id"] == instance_id
        )
        self.approvals.ensure_notification(
            task_id,
            kind="INSTANCE_DELIVERY_REJECTED",
            owner="human",
            title="Agent 交付未通过发布校验",
            message=f"实例 {instance_id} 的交付已隔离。修复后重新校验不会重跑模型步骤。",
            deep_link=f"instances/{instance_id}",
            dedupe_key=f"instance-delivery-rejected:{instance_id}:{digest_json(rejection)[:20]}",
            instance_id=instance_id,
        )
        return {
            "instance": deepcopy(instance),
            "observation": {
                "status": observation.status,
                "step_id": observation.step_id,
                "capabilities": list(observation.capabilities),
                "details": deepcopy(observation.details),
            },
            "transition": transition,
            "approval": None,
            "delivery": {"status": "REJECTED", "rejection": rejection},
        }

    def _require_delivery_command(
        self,
        task_id: str,
        instance_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        self._require_owning_adapter(instance, envelope)
        if instance["status"] != "RUNNING":
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "Only a running instance with no pending approval may publish a delivery.",
                {"current": instance["status"], "instance_id": instance_id},
            )
        pending = self.approvals.list_approvals(
            task_id=task_id,
            instance_id=instance_id,
            status="PENDING",
        )
        if pending:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A pending approval must be resolved before delivery completion.",
                {
                    "instance_id": instance_id,
                    "approval_ids": [item["approval_id"] for item in pending],
                },
            )
        self.commands.validate_task_revision(task_id, envelope.expected_revision)
        return instance

    @staticmethod
    def _require_owning_adapter(instance: dict[str, Any], envelope: CommandEnvelope) -> None:
        expected_actor_id = f"{instance['agent_type']}_adapter"
        if envelope.actor_type != "adapter" or envelope.actor_id != expected_actor_id:
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only the owning Agent adapter may publish an instance delivery.",
                {
                    "actor_type": envelope.actor_type,
                    "actor_id": envelope.actor_id,
                    "expected_actor_id": expected_actor_id,
                },
            )

    def _complete_instance_if_ready(
        self,
        task_id: str,
        instance_id: str,
        *,
        envelope: CommandEnvelope,
        require_deliveries: bool,
        candidate_manifests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """The sole instance-success gate for manual and collected deliveries."""

        self._require_delivery_command(task_id, instance_id, envelope)
        if not self._required_deliveries_satisfied(
            task_id,
            instance_id,
            candidate_manifests=candidate_manifests,
        ):
            if require_deliveries:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "Published Image assets do not satisfy the required delivery contract.",
                )
            return None
        transition = self.commands.transition_instance(
            task_id,
            instance_id,
            "SUCCEEDED",
            envelope,
        )
        self._ensure_success_notifications(task_id, instance_id, transition)
        return transition

    def _ensure_success_notifications(
        self,
        task_id: str,
        instance_id: str,
        transition: dict[str, Any],
    ) -> None:
        self.approvals.ensure_notification(
            task_id,
            kind="INSTANCE_SUCCEEDED",
            owner="human",
            title="Agent 交付已发布",
            message=f"实例 {instance_id} 的必需交付已校验并发布。",
            deep_link=f"tasks/{task_id}?tab=resources",
            dedupe_key=f"instance-succeeded:{instance_id}",
            instance_id=instance_id,
        )
        if transition["task"]["status"] in {"SUCCEEDED", "PARTIAL"}:
            self.approvals.ensure_notification(
                task_id,
                kind="TASK_SUCCEEDED",
                owner="human",
                title="主任务已完成",
                message=f"任务 {task_id} 的必需阶段均已完成。",
                deep_link=f"tasks/{task_id}",
                dedupe_key=(f"task-complete:{task_id}:" f"{transition['task']['plan_revision']}"),
            )

    def _validate_required_delivery_set(
        self,
        task_id: str,
        instance_id: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        plan = self._plan(task_id)
        card = next(item for item in plan["task_cards"] if item["instance_id"] == instance_id)
        declared = len(
            self._declared_delivery_candidates(task_id, instance_id, candidates)
        ) == len(candidates)
        if declared and self._required_deliveries_satisfied(
            task_id,
            instance_id,
            candidate_manifests=candidates,
        ):
            return
        raise HarnessError(
            "VALIDATION_ERROR",
            "Collected Image assets do not satisfy the required delivery contract.",
            {
                "required": [
                    deepcopy(item) for item in card["expected_deliveries"] if item["required"]
                ],
                "observed": [
                    {
                        "kind": item["kind"],
                        "role": item["role"],
                        "mime_type": item["mime_type"],
                        "sha256": item["sha256"],
                    }
                    for item in candidates
                ],
            },
        )

    def _required_deliveries_satisfied(
        self,
        task_id: str,
        instance_id: str,
        *,
        candidate_manifests: list[dict[str, Any]] | None = None,
    ) -> bool:
        published = candidate_manifests
        if published is None:
            published = [
                self.assets.verify_asset(task_id, item["manifest"]["asset_id"])
                for item in self.assets.list_assets(task_id)
                if item["integrity_status"] == "VERIFIED"
                and item["manifest"].get("producer_instance_id") == instance_id
            ]
        return self._required_delivery_selection(task_id, instance_id, published) is not None

    def _required_delivery_selection(
        self,
        task_id: str,
        instance_id: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        plan = self._plan(task_id)
        card = next(
            item for item in plan["task_cards"] if item["instance_id"] == instance_id
        )
        remaining = list(candidates)
        selected: list[dict[str, Any]] = []
        for expected in card["expected_deliveries"]:
            if not expected["required"]:
                continue
            match = next(
                (
                    index
                    for index, candidate in enumerate(remaining)
                    if self._delivery_matches(candidate, expected)
                ),
                None,
            )
            if match is None:
                return None
            selected.append(remaining.pop(match))
        return selected

    def _declared_delivery_candidates(
        self,
        task_id: str,
        instance_id: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        plan = self._plan(task_id)
        card = next(
            item for item in plan["task_cards"] if item["instance_id"] == instance_id
        )
        return [
            candidate
            for candidate in candidates
            if any(
                self._delivery_matches(candidate, expected)
                for expected in card["expected_deliveries"]
            )
        ]

    @staticmethod
    def _manual_delivery_batch_id(task_id: str, instance_id: str) -> str:
        digest = hashlib.sha256(f"{task_id}:{instance_id}".encode()).hexdigest()
        return f"batch_manual_{digest[:20]}"

    @staticmethod
    def _delivery_matches(candidate: dict[str, Any], expected: dict[str, Any]) -> bool:
        return (
            candidate["role"] == expected["role"]
            and candidate["kind"] == expected["kind"]
            and candidate["mime_type"] in expected["accepted_mime_types"]
        )

    def _resume_save_plan(self, intent_path: Path, crash_hook: CrashHook | None) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        actor = Actor(request["envelope"]["actor_type"], request["envelope"]["actor_id"])
        assigned_instances: list[dict[str, Any]] = []
        for raw in request["instances"]:
            instance = deepcopy(raw)
            provider = request["providers"].get(raw["instance_id"])
            if provider is not None:
                try:
                    self.commands.validate_task_revision(
                        request["task_id"], request["envelope"]["expected_revision"]
                    )
                except HarnessError as exc:
                    if exc.code == "REVISION_CONFLICT":
                        self._abort_stale_save_plan(intent_path, intent, actor, exc)
                    raise
                created = self.credentials.create_instance(
                    request["task_id"],
                    intent["creation_instances"][raw["instance_id"]],
                    provider=provider,
                    creation_id=self._derived_id(
                        "creation", intent["operation_id"], raw["instance_id"]
                    ),
                    actor=actor,
                )
                credential = created["credential"]
                instance.update(
                    {
                        "credential_pair_ref": credential["credential_pair_id"],
                        "credential_pair_revision": credential["credential_pair_revision"],
                    }
                )
                if crash_hook:
                    crash_hook(f"after_instance_created:{raw['instance_id']}")
            else:
                adapter = self.adapters.get_optional(raw["agent_type"])
                if adapter is not None and not adapter.available:
                    instance.update(
                        {
                            "credential_pair_ref": f"{raw['agent_type']}_adapter_unavailable",
                            "credential_pair_revision": 1,
                        }
                    )
            assigned_instances.append(instance)
        result = self.commands.save_plan(
            request["task_id"],
            stages=request["stages"],
            instances=assigned_instances,
            task_cards=request["task_cards"],
            envelope=CommandEnvelope.model_validate(request["envelope"]),
        )
        if crash_hook:
            crash_hook("after_plan_commit")
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return deepcopy(result)

    def _abort_stale_save_plan(
        self,
        intent_path: Path,
        intent: dict[str, Any],
        actor: Actor,
        error: HarnessError,
    ) -> None:
        intent.update({"state": "COMPENSATING", "compensation_started_at": utc_now()})
        atomic_write_json(intent_path, intent)
        compensation = []
        request = intent["request"]
        for instance_id in sorted(request["providers"]):
            creation_id = self._derived_id("creation", intent["operation_id"], instance_id)
            compensation.append(
                self.credentials.revoke_instance_creation(
                    request["task_id"],
                    creation_id,
                    revocation_id=self._derived_id("revoke", intent["operation_id"], instance_id),
                    actor=actor,
                )
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
                "compensation": compensation,
            }
        )
        atomic_write_json(intent_path, intent)

    @staticmethod
    def _raise_terminal_intent(intent: dict[str, Any]) -> NoReturn:
        error = intent["error"]
        raise HarnessError(error["code"], error["message"], deepcopy(error["details"]))

    def _resume_start(self, intent_path: Path, crash_hook: CrashHook | None) -> dict[str, Any]:
        intent = read_json(intent_path)
        task_id = intent["request"]["task_id"]
        plan = self._plan(task_id)
        if plan["task"]["status"] == "AWAITING_START_CONFIRMATION":
            plan = self.commands.confirm_start(
                task_id,
                CommandEnvelope.model_validate(intent["request"]["envelope"]),
            )["plan"]
        elif plan["task"]["status"] not in {"RUNNING", "BLOCKED_UNAVAILABLE"}:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A prepared start intent no longer belongs to an active task.",
                {"current": plan["task"]["status"]},
            )
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        instances = {item["instance_id"]: item for item in plan["instances"]}
        cards = {item["instance_id"]: item for item in plan["task_cards"]}
        launches: list[dict[str, Any]] = []
        for instance_id in intent["target_instance_ids"]:
            instance = instances.get(instance_id)
            if instance is None or instance["status"] in {
                "CANCELLED",
                "SUPERSEDED",
                "ARCHIVED",
            }:
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "A prepared start intent no longer owns a startable instance.",
                    {"instance_id": instance_id},
                )
            adapter = self.adapters.get(instance["agent_type"])
            if not adapter.available:
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE",
                    "A prepared start intent references an unavailable adapter.",
                    {"instance_id": instance_id},
                )
            self._require_valid_card(adapter, cards[instance_id])
            spec = adapter.prepare(
                PrepareRequest(
                    instance=deepcopy(instance),
                    task_card=deepcopy(cards[instance_id]),
                    task_root=task_root,
                    config_ref=task_root / "instances" / instance_id / "runtime" / "runtime.yaml",
                    credential_ref=(
                        instance["credential_pair_ref"],
                        instance["credential_pair_revision"],
                    ),
                )
            )
            launch_id = self._derived_id("launch", intent["operation_id"], instance_id)
            attempt_id = self._derived_id("attempt", intent["operation_id"], instance_id)
            launch = self.supervisor.start_instance(
                task_id,
                instance_id,
                spec,
                launch_id=launch_id,
                attempt_id=attempt_id,
            )
            if launch["state"] != "RUNNING":
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "A prepared start intent cannot reuse a non-running launch.",
                    {"instance_id": instance_id, "launch_state": launch["state"]},
                )
            if crash_hook:
                crash_hook(f"after_process_started:{instance_id}")
            adapter_result = adapter.start(instance_id, attempt_id)
            if not adapter_result.accepted:
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The Agent adapter rejected the prepared start operation.",
                    {"instance_id": instance_id},
                )
            launches.append(
                {
                    "instance_id": instance_id,
                    "launch": self._launch_summary(launch),
                    "adapter": {
                        "accepted": adapter_result.accepted,
                        "operation_id": adapter_result.operation_id,
                        "details": adapter_result.details,
                    },
                }
            )
        result = {
            "task_id": task_id,
            "launches": launches,
            "unavailable": intent["unavailable"],
        }
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return deepcopy(result)

    def _prevalidate_plan(self, request: dict[str, Any]) -> None:
        instance_ids = {item["instance_id"] for item in request["instances"]}
        unknown_providers = set(request["providers"]) - instance_ids
        if unknown_providers:
            raise HarnessError(
                "VALIDATION_ERROR",
                "A Provider mapping references an unknown instance.",
                {"instance_ids": sorted(unknown_providers)},
            )
        for instance in request["instances"]:
            adapter = self.adapters.get_optional(instance["agent_type"])
            has_provider = instance["instance_id"] in request["providers"]
            if (adapter is None or adapter.available) and not has_provider:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A runnable Agent instance requires an explicit Provider mapping.",
                    {"instance_id": instance["instance_id"]},
                )
            if adapter is not None and not adapter.available and has_provider:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "An unavailable Agent placeholder cannot consume a credential pair.",
                    {"instance_id": instance["instance_id"]},
                )
        provisional = []
        for raw in request["instances"]:
            instance = deepcopy(raw)
            if raw["instance_id"] in request["providers"]:
                instance.setdefault("credential_pair_ref", "pending_assignment")
                instance.setdefault("credential_pair_revision", 1)
            provisional.append(instance)
        self.commands.validate_plan_request(
            request["task_id"],
            stages=request["stages"],
            instances=provisional,
            task_cards=request["task_cards"],
            expected_revision=request["envelope"]["expected_revision"],
        )
        for card in request["task_cards"]:
            adapter = self.adapters.get_optional(card["agent_type"])
            if adapter is not None:
                self._require_valid_card(adapter, card)

    @staticmethod
    def _require_valid_card(adapter, card: dict[str, Any]) -> None:
        validation = adapter.validate_task_card(card)
        if not validation.valid:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Agent adapter rejected its task card.",
                {"errors": list(validation.errors)},
            )

    @staticmethod
    def _creation_summary(task_id: str, raw: dict[str, Any], created_at: str) -> dict[str, Any]:
        required = bool(raw["required"])
        return {
            **deepcopy(raw),
            "schema_version": "1.0",
            "task_id": task_id,
            "requirement_lifecycle": {
                "original_required": required,
                "first_activated_at": None,
                "authorized_downgrade": None,
            },
            "status": "CREATED",
            "process": None,
            "ui_url": None,
            "created_at": created_at,
        }

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
