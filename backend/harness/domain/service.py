"""Idempotent MainTask, plan and state-transition command handlers."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, NoReturn

from ..contracts import ContractRegistry
from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor
from ..storage.store import FileStateStore
from .commands import CommandEnvelope
from .plan import validate_plan
from .state_machine import StateMachine, stage_dependencies_authorized


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class TaskCommandService:
    """The only domain layer allowed to choose aggregate Task/Stage states."""

    def __init__(self, store: FileStateStore, contracts: ContractRegistry) -> None:
        self.store = store
        self.contracts = contracts
        self.machine = StateMachine(contracts.root / "catalogs" / "status-codes.json")
        self._command_guard_state = threading.local()

    def create_task(
        self,
        *,
        task_id: str,
        title: str,
        goal: str,
        master_owner: str,
        start_policy: str,
        input_manifest: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        request = {
            "task_id": task_id,
            "title": title,
            "goal": goal,
            "master_owner": master_owner,
            "start_policy": start_policy,
            "input_manifest": input_manifest,
        }
        return self._idempotent(
            task_id,
            "create_task",
            request,
            envelope,
            lambda: self._create_task(request, envelope),
        )

    def _create_task(self, request: dict[str, Any], envelope: CommandEnvelope) -> dict[str, Any]:
        if envelope.expected_revision != 0:
            raise HarnessError(
                "REVISION_CONFLICT",
                "Task creation requires expected revision zero.",
                {"expected_revision": envelope.expected_revision, "actual_revision": 0},
            )
        now = utc_now()
        payload = {
            "schema_version": "1.0",
            **request,
            "status": "DRAFT",
            "created_at": now,
            "updated_at": now,
            "plan_revision": 1,
        }
        result = {"task": deepcopy(payload), "revision": 1}
        self.store.task.put(
            request["task_id"],
            request["task_id"],
            payload,
            expected_revision=0,
            actor=self._actor(envelope),
            command="create_task",
            idempotency_key=envelope.idempotency_key,
            command_result=result,
            request_sha256=self._request_digest("create_task", request),
        )
        workspace_task = self.store.layout.workspace_root / "tasks" / request["task_id"]
        atomic_write_json(workspace_task / "task-summary.json", payload, mode=0o640)
        return result

    def register_input_manifest(
        self,
        task_id: str,
        input_manifest: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        request = {"task_id": task_id, "input_manifest": input_manifest}
        return self._idempotent(
            task_id,
            "register_input_manifest",
            request,
            envelope,
            lambda: self._update_task_input(request, envelope),
        )

    def rename_task(
        self,
        task_id: str,
        title: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        normalized = title.strip()
        if not normalized or len(normalized) > 256:
            raise HarnessError("VALIDATION_ERROR", "The task title is invalid.")
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError("VALIDATION_ERROR", "Only a human or Master may rename a task.")
        request = {"task_id": task_id, "title": normalized}
        return self._idempotent(
            task_id,
            "rename_task",
            request,
            envelope,
            lambda: self._rename_task(request, envelope),
        )

    def _rename_task(self, request: dict[str, Any], envelope: CommandEnvelope) -> dict[str, Any]:
        task_id = request["task_id"]
        task = self._task(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        task.update({"title": request["title"], "updated_at": utc_now()})
        plan = self.store.plan.get(task_id, task_id)
        if plan is not None:
            updated_plan = deepcopy(plan)
            updated_plan["task"] = deepcopy(task)
            return self._persist_aggregate(
                updated_plan,
                envelope,
                "rename_task",
                request,
                actual,
            )
        result = {"task": deepcopy(task), "revision": actual + 1}
        self.store.task.put(
            task_id,
            task_id,
            task,
            expected_revision=actual,
            actor=self._actor(envelope),
            command="rename_task",
            idempotency_key=envelope.idempotency_key,
            command_result=result,
            request_sha256=self._request_digest("rename_task", request),
        )
        workspace_task = self.store.layout.workspace_root / "tasks" / task_id
        atomic_write_json(workspace_task / "task-summary.json", task, mode=0o640)
        return result

    def _update_task_input(
        self, request: dict[str, Any], envelope: CommandEnvelope
    ) -> dict[str, Any]:
        task = self._task(request["task_id"])
        updated = {**task, "input_manifest": request["input_manifest"], "updated_at": utc_now()}
        result = {"task": deepcopy(updated), "revision": envelope.expected_revision + 1}
        self.store.task.put(
            request["task_id"],
            request["task_id"],
            updated,
            expected_revision=envelope.expected_revision,
            actor=self._actor(envelope),
            command="register_input_manifest",
            idempotency_key=envelope.idempotency_key,
            command_result=result,
            request_sha256=self._request_digest("register_input_manifest", request),
        )
        return result

    def save_plan(
        self,
        task_id: str,
        *,
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        task_cards: list[dict[str, Any]],
        envelope: CommandEnvelope,
        mode: str = "replace",
        expected_plan_revision: int | None = None,
    ) -> dict[str, Any]:
        if mode not in {"replace", "append", "merge"}:
            raise HarnessError("VALIDATION_ERROR", "The plan save mode is invalid.", {"mode": mode})
        request = {
            "task_id": task_id,
            "mode": mode,
            "stages": stages,
            "instances": instances,
            "task_cards": task_cards,
        }
        if expected_plan_revision is not None:
            request["expected_plan_revision"] = expected_plan_revision
        return self._idempotent(
            task_id,
            "save_plan",
            request,
            envelope,
            lambda: self._save_plan(request, envelope),
        )

    def validate_plan_request(
        self,
        task_id: str,
        *,
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        task_cards: list[dict[str, Any]],
        expected_revision: int,
        mode: str = "replace",
        expected_plan_revision: int | None = None,
    ) -> None:
        """Validate a plan without allocating credentials or committing projections."""

        if mode not in {"replace", "append", "merge"}:
            raise HarnessError("VALIDATION_ERROR", "The plan save mode is invalid.", {"mode": mode})
        task = self._task(task_id)
        self._guard_plan_save_state(task, mode)
        actual = self.store.task.revision(task_id, task_id)
        if expected_revision != actual:
            self._raise_revision(expected_revision, actual, "task", task_id)
        if expected_plan_revision is not None and expected_plan_revision != task["plan_revision"]:
            self._raise_revision(expected_plan_revision, task["plan_revision"], "plan", task_id)
        plan_revision = task["plan_revision"]
        existing = self.store.plan.get(task_id, task_id)
        if existing is not None:
            plan_revision += 1
        if mode == "append":
            if existing is None:
                raise HarnessError("TASK_NOT_FOUND", "The task does not have a saved plan.")
            self._normalize_append(task, existing, plan_revision, stages, instances, task_cards)
        elif mode == "merge":
            if existing is None:
                raise HarnessError("TASK_NOT_FOUND", "The task does not have a saved plan.")
            self._normalize_merge(task, existing, plan_revision, stages, instances, task_cards)
        else:
            self._normalize_plan(task, plan_revision, stages, instances, task_cards)

    @staticmethod
    def _guard_plan_save_state(task: dict[str, Any], mode: str) -> None:
        """Gate plan saves by semantics: replace swaps the revision, append extends it."""

        if mode == "append":
            if task["status"] in {"RUNNING", "WAITING_APPROVAL"}:
                return
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A plan can only be appended while this task is running or awaiting approval.",
                {"current": task["status"]},
            )
        if mode == "merge":
            if task["status"] in {"RUNNING", "WAITING_APPROVAL", "BLOCKED_UNAVAILABLE"}:
                return
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A plan can only be merged while this task has active instances.",
                {"current": task["status"]},
            )
        if task["status"] not in {"DRAFT", "PLANNED", "BLOCKED_UNAVAILABLE", "FAILED"}:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A plan cannot be replaced while this task state is active or terminal.",
                {"current": task["status"]},
            )

    def validate_task_revision(self, task_id: str, expected_revision: int) -> None:
        """Fail if an application workflow no longer owns its task revision."""

        with self.task_guard(task_id):
            self._task(task_id)
            actual = self.store.task.revision(task_id, task_id)
            if expected_revision != actual:
                self._raise_revision(expected_revision, actual, "task", task_id)

    def _save_plan(self, request: dict[str, Any], envelope: CommandEnvelope) -> dict[str, Any]:
        task_id = request["task_id"]
        mode = request.get("mode", "replace")
        task = self._task(task_id)
        self._guard_plan_save_state(task, mode)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        expected_plan_revision = request.get("expected_plan_revision")
        if expected_plan_revision is not None and expected_plan_revision != task["plan_revision"]:
            self._raise_revision(expected_plan_revision, task["plan_revision"], "plan", task_id)
        business_revision = task["plan_revision"]
        existing = self.store.plan.get(task_id, task_id)
        if existing is not None:
            business_revision += 1
        if mode == "append":
            if existing is None:
                raise HarnessError("TASK_NOT_FOUND", "The task does not have a saved plan.")
            plan = self._normalize_append(
                task,
                existing,
                business_revision,
                request["stages"],
                request["instances"],
                request["task_cards"],
            )
            self._activate_current_stages(plan, utc_now())
            target = self._aggregate_task(plan, preserve_start_confirmation=False)
            if target != plan["task"]["status"]:
                self.machine.transition("main_task", plan["task"]["status"], target)
                plan["task"]["status"] = target
            plan["task"]["updated_at"] = utc_now()
            return self._persist_aggregate(plan, envelope, "save_plan", request, actual)
        if mode == "merge":
            if existing is None:
                raise HarnessError("TASK_NOT_FOUND", "The task does not have a saved plan.")
            plan = self._normalize_merge(
                task,
                existing,
                business_revision,
                request["stages"],
                request["instances"],
                request["task_cards"],
            )
            plan["task"]["updated_at"] = utc_now()
            return self._persist_aggregate(
                plan,
                envelope,
                "save_plan",
                request,
                actual,
                skip_unchanged_projections=True,
            )
        plan = self._normalize_plan(
            task,
            business_revision,
            request["stages"],
            request["instances"],
            request["task_cards"],
        )
        if task["status"] != "PLANNED":
            self.machine.transition("main_task", task["status"], "PLANNED")
        plan["task"]["status"] = "PLANNED"
        if task["start_policy"] == "manual":
            self.machine.transition("main_task", "PLANNED", "AWAITING_START_CONFIRMATION")
            plan["task"]["status"] = "AWAITING_START_CONFIRMATION"
        else:
            self._activate_current_stages(plan, utc_now())
            target = self._aggregate_task(plan, preserve_start_confirmation=False)
            self._validate_task_reaggregation("PLANNED", target)
            plan["task"]["status"] = target
        plan["task"]["updated_at"] = utc_now()
        return self._persist_aggregate(plan, envelope, "save_plan", request, actual)

    def confirm_start(self, task_id: str, envelope: CommandEnvelope) -> dict[str, Any]:
        request = {"task_id": task_id}
        return self._idempotent(
            task_id,
            "confirm_start",
            request,
            envelope,
            lambda: self._confirm_start(request, envelope),
        )

    def _confirm_start(self, request: dict[str, Any], envelope: CommandEnvelope) -> dict[str, Any]:
        task_id = request["task_id"]
        plan = self._plan(task_id)
        task = plan["task"]
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        if task["status"] != "AWAITING_START_CONFIRMATION":
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "Only a task awaiting manual start can be confirmed.",
                {"current": task["status"]},
            )
        self._activate_current_stages(plan, utc_now())
        target = self._aggregate_task(plan, preserve_start_confirmation=False)
        self._validate_task_reaggregation(task["status"], target)
        task["status"] = target
        task["updated_at"] = utc_now()
        return self._persist_aggregate(plan, envelope, "confirm_start", request, actual)

    def set_manual_finished(
        self,
        task_id: str,
        instance_id: str,
        manual_finished: bool,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        """Set the reversible human completion gate for one Image instance."""

        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "manual_finished": manual_finished,
        }
        return self._idempotent(
            task_id,
            "set_manual_finished",
            request,
            envelope,
            lambda: self._set_manual_finished(request, envelope),
        )

    def _set_manual_finished(
        self, request: dict[str, Any], envelope: CommandEnvelope
    ) -> dict[str, Any]:
        if envelope.actor_type != "human":
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only a human may change the manual completion gate.",
            )
        task_id = request["task_id"]
        plan = self._plan(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        instance = next(
            (
                item
                for item in plan["instances"]
                if item["instance_id"] == request["instance_id"]
            ),
            None,
        )
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        if instance["agent_type"] != "image":
            raise HarnessError(
                "VALIDATION_ERROR",
                "The manual completion gate only applies to Image instances.",
                {"instance_id": request["instance_id"]},
            )
        instance["manual_finished"] = request["manual_finished"]
        if request["manual_finished"]:
            instance["manual_business_status"] = "COMPLETED"
        else:
            instance.pop("manual_business_status", None)
        plan["task"]["updated_at"] = utc_now()
        return self._persist_aggregate(
            plan, envelope, "set_manual_finished", request, actual
        )

    def set_manual_business_status(
        self,
        task_id: str,
        instance_id: str,
        business_status: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        """Set a reversible board status until the runtime reports a real transition."""

        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "business_status": business_status,
        }
        return self._idempotent(
            task_id,
            "set_manual_business_status",
            request,
            envelope,
            lambda: self._set_manual_business_status(request, envelope),
        )

    def _set_manual_business_status(
        self, request: dict[str, Any], envelope: CommandEnvelope
    ) -> dict[str, Any]:
        allowed = {"TODO", "RUNNING", "WAITING_APPROVAL", "COMPLETED"}
        if request["business_status"] not in allowed:
            raise HarnessError("VALIDATION_ERROR", "The WorkItem business status is invalid.")
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only a human or Master may change the WorkItem business status.",
            )
        task_id = request["task_id"]
        plan = self._plan(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        instance = next(
            (item for item in plan["instances"] if item["instance_id"] == request["instance_id"]),
            None,
        )
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        instance["manual_business_status"] = request["business_status"]
        if instance["agent_type"] == "image":
            instance["manual_finished"] = request["business_status"] == "COMPLETED"
        plan["task"]["updated_at"] = utc_now()
        return self._persist_aggregate(
            plan, envelope, "set_manual_business_status", request, actual
        )

    def transition_instance(
        self,
        task_id: str,
        instance_id: str,
        target_status: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "target_status": target_status,
        }
        return self._idempotent(
            task_id,
            "transition_instance",
            request,
            envelope,
            lambda: self._transition_instance(request, envelope),
        )

    def reject_instance_delivery(
        self,
        task_id: str,
        instance_id: str,
        rejection: dict[str, Any],
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "rejection": deepcopy(rejection),
        }
        return self._idempotent(
            task_id,
            "reject_instance_delivery",
            request,
            envelope,
            lambda: self._reject_instance_delivery(request, envelope),
        )

    def set_approval_mode(
        self,
        task_id: str,
        instance_id: str,
        approval_mode: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "approval_mode": approval_mode,
        }
        return self._idempotent(
            task_id,
            "set_approval_mode",
            request,
            envelope,
            lambda: self._set_approval_mode(request, envelope),
        )

    def _set_approval_mode(
        self, request: dict[str, Any], envelope: CommandEnvelope
    ) -> dict[str, Any]:
        if request["approval_mode"] not in {"human", "master"}:
            raise HarnessError("VALIDATION_ERROR", "The approval mode is invalid.")
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human or Master may change approval routing."
            )
        task_id = request["task_id"]
        plan = self._plan(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        instance = next(
            (item for item in plan["instances"] if item["instance_id"] == request["instance_id"]),
            None,
        )
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        instance["approval_mode"] = request["approval_mode"]
        plan["task"]["updated_at"] = utc_now()
        return self._persist_aggregate(plan, envelope, "set_approval_mode", request, actual)

    def _transition_instance(
        self, request: dict[str, Any], envelope: CommandEnvelope
    ) -> dict[str, Any]:
        task_id = request["task_id"]
        plan = self._plan(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        instance = next(
            (item for item in plan["instances"] if item["instance_id"] == request["instance_id"]),
            None,
        )
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        self.machine.transition("agent_instance", instance["status"], request["target_status"])
        instance["status"] = request["target_status"]
        cleared_manual_status = instance.pop("manual_business_status", None)
        if cleared_manual_status is not None and instance["agent_type"] == "image":
            instance["manual_finished"] = False
        if request["target_status"] in {
            "SUCCEEDED",
            "FAILED_TO_START",
            "FAILED",
            "CRASHED",
            "CANCELLED",
            "SUPERSEDED",
            "ARCHIVED",
        }:
            instance.pop("delivery_rejection", None)
        if request["target_status"] in {"STARTING", "RUNNING"}:
            self._activate_lifecycle(instance, utc_now())
        self._refresh_stages(plan, utc_now())
        task = plan["task"]
        target = self._aggregate_task(plan, preserve_start_confirmation=False)
        if target != task["status"]:
            self.machine.transition("main_task", task["status"], target)
            task["status"] = target
        task["updated_at"] = utc_now()
        return self._persist_aggregate(plan, envelope, "transition_instance", request, actual)

    def _reject_instance_delivery(
        self, request: dict[str, Any], envelope: CommandEnvelope
    ) -> dict[str, Any]:
        task_id = request["task_id"]
        plan = self._plan(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        instance = next(
            (item for item in plan["instances"] if item["instance_id"] == request["instance_id"]),
            None,
        )
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        if (
            envelope.actor_type != "adapter"
            or envelope.actor_id != f"{instance['agent_type']}_adapter"
        ):
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only the owning Agent adapter may reject a collected delivery.",
            )
        self.machine.transition("agent_instance", instance["status"], "FAILED")
        rejection = deepcopy(request["rejection"])
        rejection["rejected_at"] = utc_now()
        rejection["retryable"] = True
        instance["status"] = "FAILED"
        cleared_manual_status = instance.pop("manual_business_status", None)
        if cleared_manual_status is not None and instance["agent_type"] == "image":
            instance["manual_finished"] = False
        instance["delivery_rejection"] = rejection
        self._refresh_stages(plan, utc_now())
        task = plan["task"]
        target = self._aggregate_task(plan, preserve_start_confirmation=False)
        if target != task["status"]:
            self.machine.transition("main_task", task["status"], target)
            task["status"] = target
        task["updated_at"] = utc_now()
        return self._persist_aggregate(plan, envelope, "reject_instance_delivery", request, actual)

    def cancel_task(self, task_id: str, envelope: CommandEnvelope) -> dict[str, Any]:
        request = {"task_id": task_id}
        return self._idempotent(
            task_id,
            "cancel_task",
            request,
            envelope,
            lambda: self._cancel_task(request, envelope),
        )

    def _cancel_task(self, request: dict[str, Any], envelope: CommandEnvelope) -> dict[str, Any]:
        task_id = request["task_id"]
        task = self._task(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        self.machine.transition("main_task", task["status"], "CANCELLED")
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            task = {**task, "status": "CANCELLED", "updated_at": utc_now()}
            result = {"task": deepcopy(task), "revision": actual + 1}
            self.store.task.put(
                task_id,
                task_id,
                task,
                expected_revision=actual,
                actor=self._actor(envelope),
                command="cancel_task",
                idempotency_key=envelope.idempotency_key,
                command_result=result,
                request_sha256=self._request_digest("cancel_task", request),
            )
            return result
        plan = deepcopy(plan)
        transitions = self.machine.catalog["agent_instance"]["transitions"]
        for instance in plan["instances"]:
            if "CANCELLED" in transitions[instance["status"]]:
                instance["status"] = "CANCELLED"
                if instance.pop("manual_business_status", None) is not None:
                    instance["manual_finished"] = False
        stage_transitions = self.machine.catalog["stage"]["transitions"]
        for stage in plan["stages"]:
            if "CANCELLED" in stage_transitions[stage["status"]]:
                stage["status"] = "CANCELLED"
        plan["task"]["status"] = "CANCELLED"
        plan["task"]["updated_at"] = utc_now()
        return self._persist_aggregate(plan, envelope, "cancel_task", request, actual)

    def downgrade_instance(
        self,
        task_id: str,
        instance_id: str,
        reason: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        request = {"task_id": task_id, "instance_id": instance_id, "reason": reason}
        return self._idempotent(
            task_id,
            "downgrade_instance",
            request,
            envelope,
            lambda: self._downgrade_instance(request, envelope),
        )

    def _downgrade_instance(
        self, request: dict[str, Any], envelope: CommandEnvelope
    ) -> dict[str, Any]:
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human or Master may authorize a downgrade."
            )
        task_id = request["task_id"]
        plan = self._plan(task_id)
        actual = self.store.task.revision(task_id, task_id)
        if envelope.expected_revision != actual:
            self._raise_revision(envelope.expected_revision, actual, "task", task_id)
        instance = next(
            (item for item in plan["instances"] if item["instance_id"] == request["instance_id"]),
            None,
        )
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        lifecycle = instance["requirement_lifecycle"]
        if (
            not instance["required"]
            or not lifecycle["original_required"]
            or lifecycle["first_activated_at"] is None
        ):
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "Only an activated, originally required instance can be downgraded.",
            )
        new_plan_revision = max(2, plan["task"]["plan_revision"] + 1)
        authorization = {
            "authorization_id": f"auth_{uuid.uuid4().hex}",
            "authorized_at": utc_now(),
            "authorized_by_type": envelope.actor_type,
            "authorized_by_id": envelope.actor_id,
            "plan_revision": new_plan_revision,
            "reason": request["reason"],
        }
        instance["required"] = False
        lifecycle["authorized_downgrade"] = authorization
        stage = next(item for item in plan["stages"] if item["stage_id"] == instance["stage_id"])
        remaining_required = [
            item
            for item in plan["instances"]
            if item["stage_id"] == stage["stage_id"] and item["required"]
        ]
        if not remaining_required:
            stage["required"] = False
            self._activate_lifecycle(stage, lifecycle["first_activated_at"])
            stage["requirement_lifecycle"]["authorized_downgrade"] = authorization
        plan["task"]["plan_revision"] = new_plan_revision
        self._refresh_stages(plan, utc_now())
        target = self._aggregate_task(plan, preserve_start_confirmation=False)
        if target != plan["task"]["status"]:
            self.machine.transition("main_task", plan["task"]["status"], target)
            plan["task"]["status"] = target
        plan["task"]["updated_at"] = utc_now()
        return self._persist_aggregate(plan, envelope, "downgrade_instance", request, actual)

    def _normalize_plan(
        self,
        task: dict[str, Any],
        plan_revision: int,
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        task_cards: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = utc_now()
        normalized_stages = []
        for raw in sorted(deepcopy(stages), key=lambda item: item["position"]):
            required = bool(raw["required"])
            normalized_stages.append(
                {
                    **raw,
                    "schema_version": "1.0",
                    "task_id": task["task_id"],
                    "required": required,
                    "requirement_lifecycle": {
                        "original_required": required,
                        "first_activated_at": None,
                        "authorized_downgrade": None,
                    },
                    "status": "PENDING",
                }
            )
        normalized_instances = []
        for raw in deepcopy(instances):
            required = bool(raw["required"])
            normalized_instances.append(
                {
                    **raw,
                    "schema_version": "1.0",
                    "task_id": task["task_id"],
                    "required": required,
                    "requirement_lifecycle": {
                        "original_required": required,
                        "first_activated_at": None,
                        "authorized_downgrade": None,
                    },
                    "status": (
                        "READY"
                        if raw["agent_type"] in {"general", "image", "ppt"}
                        else "UNAVAILABLE"
                    ),
                    "manual_finished": False,
                    "process": None,
                    "ui_url": None,
                    "created_at": now,
                }
            )
        normalized_cards = [
            {
                **deepcopy(raw),
                "schema_version": raw.get("schema_version", "1.0"),
                "task_id": task["task_id"],
                "created_at": now,
            }
            for raw in task_cards
        ]
        normalized_task = {
            **deepcopy(task),
            "status": "PLANNED",
            "plan_revision": plan_revision,
            "updated_at": now,
        }
        plan = {
            "schema_version": "1.0",
            "task": normalized_task,
            "stages": normalized_stages,
            "instances": normalized_instances,
            "task_cards": normalized_cards,
        }
        self._refresh_stages(plan, now, activate_new=False)
        validate_plan(self.contracts, plan)
        return plan

    def _normalize_append(
        self,
        task: dict[str, Any],
        existing_plan: dict[str, Any],
        plan_revision: int,
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        task_cards: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Merge new stages/instances/cards into the landed plan revision.

        The payload must only carry new objects: referencing an already landed
        stage, instance, or task card id is rejected so an append can never
        mutate work that is already on the board. New stages keep the plan
        topology rule (parallel same-type stages, Image-to-PPT chains only),
        which validate_plan enforces on the merged revision.
        """

        landed_stage_ids = {item["stage_id"] for item in existing_plan["stages"]}
        landed_instance_ids = {item["instance_id"] for item in existing_plan["instances"]}
        landed_card_ids = {item["card_id"] for item in existing_plan["task_cards"]}
        for raw in stages:
            if raw["stage_id"] in landed_stage_ids:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A plan append cannot reference or modify a landed stage.",
                    {"stage_id": raw["stage_id"]},
                )
        for raw in instances:
            if raw["instance_id"] in landed_instance_ids:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A plan append cannot reference or modify a landed instance.",
                    {"instance_id": raw["instance_id"]},
                )
        for raw in task_cards:
            if raw["card_id"] in landed_card_ids:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A plan append cannot reference or modify a landed task card.",
                    {"card_id": raw["card_id"]},
                )
        now = utc_now()
        normalized_stages = []
        for raw in sorted(deepcopy(stages), key=lambda item: item["position"]):
            required = bool(raw["required"])
            normalized_stages.append(
                {
                    **raw,
                    "schema_version": "1.0",
                    "task_id": task["task_id"],
                    "required": required,
                    "requirement_lifecycle": {
                        "original_required": required,
                        "first_activated_at": None,
                        "authorized_downgrade": None,
                    },
                    "status": "PENDING",
                }
            )
        normalized_instances = []
        for raw in deepcopy(instances):
            required = bool(raw["required"])
            normalized_instances.append(
                {
                    **raw,
                    "schema_version": "1.0",
                    "task_id": task["task_id"],
                    "required": required,
                    "requirement_lifecycle": {
                        "original_required": required,
                        "first_activated_at": None,
                        "authorized_downgrade": None,
                    },
                    "status": (
                        "READY"
                        if raw["agent_type"] in {"general", "image", "ppt"}
                        else "UNAVAILABLE"
                    ),
                    "manual_finished": False,
                    "process": None,
                    "ui_url": None,
                    "created_at": now,
                }
            )
        normalized_cards = [
            {
                **deepcopy(raw),
                "schema_version": raw.get("schema_version", "1.0"),
                "task_id": task["task_id"],
                "created_at": now,
            }
            for raw in task_cards
        ]
        plan = {
            "schema_version": "1.0",
            "task": {
                **deepcopy(task),
                "plan_revision": plan_revision,
                "updated_at": now,
            },
            "stages": [*deepcopy(existing_plan["stages"]), *normalized_stages],
            "instances": [*deepcopy(existing_plan["instances"]), *normalized_instances],
            "task_cards": [*deepcopy(existing_plan["task_cards"]), *normalized_cards],
        }
        self._refresh_stages(plan, now, activate_new=False)
        validate_plan(self.contracts, plan)
        return plan

    def _normalize_merge(
        self,
        task: dict[str, Any],
        existing_plan: dict[str, Any],
        plan_revision: int,
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        task_cards: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Apply card revisions to not-yet-started instances without touching started ones.

        A merge keeps the landed stage/instance projections exactly as they are:
        an instance that already entered its start lifecycle keeps its record and
        its landed task card, so a running Agent is never replaced or rewritten.
        Only the task cards of instances still waiting to start (READY or
        UNAVAILABLE) are refreshed from the incoming payload. The merge cannot
        change the plan topology: stage, instance, and card sets must match the
        landed revision.
        """

        landed_stage_by_id = {item["stage_id"]: item for item in existing_plan["stages"]}
        incoming_stage_by_id = {item["stage_id"]: item for item in stages}
        if set(incoming_stage_by_id) != set(landed_stage_by_id):
            raise HarnessError(
                "VALIDATION_ERROR",
                "A plan merge cannot add or remove stages while instances are started.",
            )
        landed_instance_by_id = {item["instance_id"]: item for item in existing_plan["instances"]}
        incoming_instance_by_id = {item["instance_id"]: item for item in instances}
        if set(incoming_instance_by_id) != set(landed_instance_by_id):
            raise HarnessError(
                "VALIDATION_ERROR",
                "A plan merge cannot add or remove instances while instances are started.",
            )
        for instance_id, raw in incoming_instance_by_id.items():
            landed = landed_instance_by_id[instance_id]
            if raw["stage_id"] != landed["stage_id"] or raw["agent_type"] != landed["agent_type"]:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A plan merge cannot rewire a landed instance.",
                    {"instance_id": instance_id},
                )
        for stage_id, raw in incoming_stage_by_id.items():
            landed_stage = landed_stage_by_id[stage_id]
            if raw["type"] != landed_stage["type"] or set(raw["instance_ids"]) != set(
                landed_stage["instance_ids"]
            ):
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A plan merge cannot rewire a landed stage.",
                    {"stage_id": stage_id},
                )
        landed_card_by_id = {item["card_id"]: item for item in existing_plan["task_cards"]}
        incoming_card_by_id = {item["card_id"]: item for item in task_cards}
        if set(incoming_card_by_id) != set(landed_card_by_id):
            raise HarnessError(
                "VALIDATION_ERROR",
                "A plan merge cannot add or remove task cards while instances are started.",
            )
        now = utc_now()
        merged_cards = []
        for landed_card in existing_plan["task_cards"]:
            card_id = landed_card["card_id"]
            raw = incoming_card_by_id[card_id]
            if raw["instance_id"] != landed_card["instance_id"]:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A plan merge cannot move a task card to another instance.",
                    {"card_id": card_id},
                )
            instance = landed_instance_by_id[landed_card["instance_id"]]
            if instance["status"] in {"READY", "UNAVAILABLE"}:
                if raw["revision"] < landed_card["revision"]:
                    raise HarnessError(
                        "REVISION_CONFLICT",
                        "A plan merge cannot restore an older TaskCard revision.",
                        {
                            "card_id": card_id,
                            "expected_revision": landed_card["revision"],
                            "actual_revision": raw["revision"],
                        },
                    )
                if raw["revision"] == landed_card["revision"]:
                    merged_cards.append(deepcopy(landed_card))
                    continue
                merged_cards.append(
                    {
                        **deepcopy(raw),
                        "schema_version": raw.get("schema_version", "1.0"),
                        "task_id": task["task_id"],
                        "created_at": raw.get("created_at", now),
                    }
                )
            else:
                merged_cards.append(deepcopy(landed_card))
        plan = {
            "schema_version": "1.0",
            "task": {
                **deepcopy(task),
                "plan_revision": plan_revision,
                "updated_at": now,
            },
            "stages": deepcopy(existing_plan["stages"]),
            "instances": deepcopy(existing_plan["instances"]),
            "task_cards": merged_cards,
        }
        validate_plan(self.contracts, plan)
        return plan

    def _refresh_stages(self, plan: dict[str, Any], now: str, *, activate_new: bool = True) -> None:
        stage_by_id = {item["stage_id"]: item for item in plan["stages"]}
        instance_by_id = {item["instance_id"]: item for item in plan["instances"]}
        for stage in sorted(plan["stages"], key=lambda item: item["position"]):
            before = stage["status"]
            target = self.machine.aggregate_stage(stage, stage_by_id, instance_by_id)
            dependencies_ready = stage_dependencies_authorized(
                stage, stage_by_id, instance_by_id
            )
            if activate_new and dependencies_ready and stage["type"] == "ppt":
                self._activate_lifecycle(stage, now)
                for instance_id in stage["instance_ids"]:
                    self._activate_lifecycle(instance_by_id[instance_id], now)
            if target != before and before not in {"SKIPPED", "CANCELLED"}:
                self._validate_stage_reaggregation(before, target)
            stage["status"] = target

    def _validate_stage_reaggregation(self, current: str, target: str) -> None:
        """Validate plan-revision reaggregation through catalog-listed intermediate states."""

        if current == "PENDING" and target == "RUNNING":
            self.machine.transition("stage", "PENDING", "READY")
            self.machine.transition("stage", "READY", "RUNNING")
            return
        if current == "FAILED" and target == "SUCCEEDED":
            self.machine.transition("stage", "FAILED", "RUNNING")
            self.machine.transition("stage", "RUNNING", "SUCCEEDED")
            return
        if current == "FAILED" and target == "SKIPPED":
            self.machine.transition("stage", "FAILED", "READY")
            self.machine.transition("stage", "READY", "SKIPPED")
            return
        self.machine.transition("stage", current, target)

    def _activate_current_stages(self, plan: dict[str, Any], now: str) -> None:
        stage_by_id = {item["stage_id"]: item for item in plan["stages"]}
        instance_by_id = {item["instance_id"]: item for item in plan["instances"]}
        for stage in sorted(plan["stages"], key=lambda item: item["position"]):
            if not stage_dependencies_authorized(stage, stage_by_id, instance_by_id):
                continue
            self._activate_lifecycle(stage, now)
            for instance_id in stage["instance_ids"]:
                self._activate_lifecycle(instance_by_id[instance_id], now)
            target = self.machine.aggregate_stage(stage, stage_by_id, instance_by_id)
            if target != stage["status"]:
                self.machine.transition("stage", stage["status"], target)
                stage["status"] = target

    @staticmethod
    def _activate_lifecycle(item: dict[str, Any], now: str) -> None:
        lifecycle = item["requirement_lifecycle"]
        if lifecycle["first_activated_at"] is None:
            lifecycle["first_activated_at"] = now

    def _aggregate_task(self, plan: dict[str, Any], *, preserve_start_confirmation: bool) -> str:
        return self.machine.aggregate_task(
            plan["task"],
            plan["stages"],
            plan["instances"],
            preserve_start_confirmation=preserve_start_confirmation,
        )

    def _validate_task_reaggregation(self, current: str, target: str) -> None:
        """Validate zero-work completion through the catalog's RUNNING state."""

        if current in {"PLANNED", "AWAITING_START_CONFIRMATION"} and target == "SUCCEEDED":
            self.machine.transition("main_task", current, "RUNNING")
            self.machine.transition("main_task", "RUNNING", "SUCCEEDED")
            return
        self.machine.transition("main_task", current, target)

    def _persist_aggregate(
        self,
        plan: dict[str, Any],
        envelope: CommandEnvelope,
        command: str,
        request: dict[str, Any],
        expected_task_revision: int,
        *,
        skip_unchanged_projections: bool = False,
    ) -> dict[str, Any]:
        validate_plan(self.contracts, plan)
        task_id = plan["task"]["task_id"]
        actor = self._actor(envelope)
        next_plan_revision = self.store.plan.revision(task_id, task_id) + 1
        result = {
            "task": deepcopy(plan["task"]),
            "plan": deepcopy(plan),
            "task_revision": expected_task_revision + 1,
            "store_plan_revision": next_plan_revision,
        }
        self.store.plan.put(
            task_id,
            task_id,
            deepcopy(plan),
            expected_revision=next_plan_revision - 1,
            actor=actor,
            command=command,
            idempotency_key=envelope.idempotency_key,
            command_result=result,
            request_sha256=self._request_digest(command, request),
        )
        self.store.retire_plan_projections(task_id, plan)
        task_wrapper = self.store.task.put(
            task_id,
            task_id,
            deepcopy(plan["task"]),
            expected_revision=expected_task_revision,
            actor=actor,
            command=command,
            idempotency_key=envelope.idempotency_key,
        )
        for stage in plan["stages"]:
            if (
                skip_unchanged_projections
                and self.store.stage.get(task_id, stage["stage_id"]) == stage
            ):
                continue
            self.store.stage.put(
                task_id,
                stage["stage_id"],
                deepcopy(stage),
                expected_revision=self.store.stage.revision(task_id, stage["stage_id"]),
                actor=actor,
                command=command,
                idempotency_key=envelope.idempotency_key,
            )
        for instance in plan["instances"]:
            if (
                skip_unchanged_projections
                and self.store.instance.get(task_id, instance["instance_id"]) == instance
            ):
                continue
            self.store.instance.put(
                task_id,
                instance["instance_id"],
                deepcopy(instance),
                expected_revision=self.store.instance.revision(task_id, instance["instance_id"]),
                actor=actor,
                command=command,
                idempotency_key=envelope.idempotency_key,
            )
        workspace_task = self.store.layout.workspace_root / "tasks" / task_id
        atomic_write_json(workspace_task / "task-summary.json", plan["task"], mode=0o640)
        if task_wrapper["revision"] != result["task_revision"]:
            raise RuntimeError("task projection revision diverged from the aggregate commit")
        return result

    def _idempotent(
        self,
        scope: str,
        command: str,
        request: dict[str, Any],
        envelope: CommandEnvelope,
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        validate_identifier(scope, "task_id")
        with self.task_guard(scope):
            existing = self.store.idempotency.lookup(
                scope, envelope.idempotency_key, command, request
            )
            if existing is not None:
                return existing
            request_sha256 = self._request_digest(command, request)
            committed = self.store.lookup_committed_command_result(
                scope,
                envelope.idempotency_key,
                command,
                request_sha256,
            )
            if committed is not None:
                return self.store.idempotency.remember_digest(
                    scope,
                    envelope.idempotency_key,
                    command,
                    request_sha256,
                    committed,
                )
            result = operation()
            committed = self.store.lookup_committed_command_result(
                scope,
                envelope.idempotency_key,
                command,
                request_sha256,
            )
            if committed is None or committed != result:
                raise RuntimeError("command returned without an authoritative result commit")
            return self.store.idempotency.remember_digest(
                scope,
                envelope.idempotency_key,
                command,
                request_sha256,
                committed,
            )

    @contextmanager
    def task_guard(self, task_id: str) -> Iterator[None]:
        """Hold the domain command lock across one application-level workflow."""

        validate_identifier(task_id, "task_id")
        guarded = getattr(self._command_guard_state, "task_ids", frozenset())
        if task_id in guarded:
            yield
            return
        with FileLock(
            self.store.layout.control_root / "locks" / f"command-{task_id}.lock",
            self.store.lock_timeout_seconds,
        ):
            self._command_guard_state.task_ids = guarded | {task_id}
            try:
                yield
            finally:
                self._command_guard_state.task_ids = guarded

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.store.task.get(task_id, task_id)
        if task is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        return deepcopy(task)

    def _plan(self, task_id: str) -> dict[str, Any]:
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            raise HarnessError("TASK_NOT_FOUND", "The task does not have a saved plan.")
        return deepcopy(plan)

    @staticmethod
    def _actor(envelope: CommandEnvelope) -> Actor:
        return Actor(envelope.actor_type, envelope.actor_id)

    def _request_digest(self, command: str, request: dict[str, Any]) -> str:
        return self.store.idempotency.request_digest(command, request)

    @staticmethod
    def _raise_revision(expected: int, actual: int, kind: str, object_id: str) -> NoReturn:
        raise HarnessError(
            "REVISION_CONFLICT",
            "The object revision changed before this command committed.",
            {
                "object_type": kind,
                "object_id": object_id,
                "expected_revision": expected,
                "actual_revision": actual,
            },
        )
