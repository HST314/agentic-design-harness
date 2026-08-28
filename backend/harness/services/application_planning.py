"""Plan persistence and startup recovery application use cases."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

from ..adapters import PrepareRequest
from ..adapters.types import AgentInstanceSnapshot, TaskCard
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import atomic_write_json, read_json
from ..storage.repository import Actor, utc_now

if TYPE_CHECKING:
    from ..adapters import AgentAdapter

CrashHook = Callable[[str], None]


class ApplicationPlanningMixin:
    """Plan-specific extension point kept separate from Agent delivery flows."""

    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    def _resume_save_plan(self, intent_path: Path, crash_hook: CrashHook | None) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        try:
            result = self.commands.save_plan(
                request["task_id"],
                stages=request["stages"],
                instances=request["instances"],
                task_cards=request["task_cards"],
                envelope=CommandEnvelope.model_validate(request["envelope"]),
                mode=request.get("mode", "replace"),
                expected_plan_revision=request.get("expected_plan_revision"),
            )
        except HarnessError as exc:
            if exc.code == "REVISION_CONFLICT":
                self._abort_stale_save_plan(intent_path, intent, exc)
            raise
        if crash_hook:
            crash_hook("after_plan_commit")
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return deepcopy(result)

    def _abort_stale_save_plan(
        self,
        intent_path: Path,
        intent: dict[str, Any],
        error: HarnessError,
    ) -> None:
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

    @staticmethod
    def _raise_terminal_intent(intent: dict[str, Any]) -> NoReturn:
        error = intent["error"]
        raise HarnessError(error["code"], error["message"], deepcopy(error["details"]))

    def _resume_start(self, intent_path: Path, crash_hook: CrashHook | None) -> dict[str, Any]:
        intent = self._migrate_start_intent(intent_path, read_json(intent_path))
        if intent["state"] == "COMMITTED":
            return self._start_operation_summary(intent)
        if intent["state"] == "ABORTED":
            return self._start_operation_summary(intent)
        if intent["state"] == "RETRYABLE_FAILED":
            return self._start_operation_summary(intent)
        task_id = intent["request"]["task_id"]
        self.task_config.lock_for_start(task_id)
        self._confirm_start_intent(intent_path)
        intent = read_json(intent_path)
        plan = self._plan(task_id)
        if plan["task"]["status"] not in {"RUNNING", "BLOCKED_UNAVAILABLE", "FAILED"}:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A prepared start intent no longer belongs to an active task.",
                {"current": plan["task"]["status"]},
            )
        now = utc_now()
        intent.update({"state": "RUNNING", "updated_at": now})
        atomic_write_json(intent_path, intent)
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        cards = {item["instance_id"]: item for item in plan["task_cards"]}
        launches: list[dict[str, Any]] = []
        for instance_id in intent["target_instance_ids"]:
            progress = intent["instance_progress"][instance_id]
            if progress["state"] == "RUNNING":
                launches.append(deepcopy(progress["result"]))
                continue
            instance = self._instance(task_id, instance_id)
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
            try:
                result = self._resume_start_instance(
                    intent_path,
                    intent,
                    progress,
                    instance,
                    cards[instance_id],
                    task_root,
                    crash_hook,
                )
            except HarnessError as exc:
                self._record_start_failure(intent_path, intent, progress, instance_id, exc)
                return self._start_operation_summary(intent)
            launches.append(result)
        result = {
            "schema_version": "1.1",
            "operation_id": intent["operation_id"],
            "task_id": task_id,
            "launches": launches,
            "unavailable": intent["unavailable"],
        }
        completed_at = utc_now()
        intent.update(
            {
                "state": "COMMITTED",
                "committed_at": completed_at,
                "completed_at": completed_at,
                "updated_at": completed_at,
                "last_error": None,
                "result": result,
            }
        )
        atomic_write_json(intent_path, intent)
        return self._start_operation_summary(intent)

    def _resume_start_instance(
        self,
        intent_path: Path,
        intent: dict[str, Any],
        progress: dict[str, Any],
        instance: dict[str, Any],
        card: dict[str, Any],
        task_root: Path,
        crash_hook: CrashHook | None,
    ) -> dict[str, Any]:
        task_id = intent["request"]["task_id"]
        instance_id = instance["instance_id"]
        previous_stage = progress["side_effect_stage"]
        progress["attempt"] = max(1, int(progress.get("attempt", 0)))
        attempt = progress["attempt"]
        if progress["state"] == "PENDING" and progress.get("last_error") is None:
            progress["attempt"] = attempt
        self._restore_startable_instance(task_id, instance_id, intent["operation_id"], attempt)
        instance = self._instance(task_id, instance_id)
        progress.update({"state": "PREPARING", "updated_at": utc_now()})
        atomic_write_json(intent_path, intent)
        adapter = self.adapters.get(instance["agent_type"])
        if not adapter.available:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "A prepared start intent references an unavailable adapter.",
                {"instance_id": instance_id},
            )
        self._require_valid_card(adapter, cast(TaskCard, card))
        if instance["agent_type"] == "ppt" and progress.get("side_effect_stage") == "NONE":
            self._require_ppt_start_gate(task_id, instance_id)
        if instance["agent_type"] == "image":
            self.runtime_settings.ensure_before_start_locked(task_id, instance_id)
        spec = adapter.prepare(
            PrepareRequest(
                instance=cast(AgentInstanceSnapshot, deepcopy(instance)),
                task_card=cast(TaskCard, deepcopy(card)),
                task_root=task_root,
                config_ref=task_root / "instances" / instance_id / "runtime" / "runtime.yaml",
            )
        )
        launch_id = self._derived_id(
            "launch", intent["operation_id"], f"{instance_id}-{attempt}"
        )
        attempt_id = self._derived_id(
            "attempt", intent["operation_id"], f"{instance_id}-{attempt}"
        )
        progress.update(
            {
                "state": "PROCESS_STARTING",
                "launch_id": launch_id,
                "updated_at": utc_now(),
            }
        )
        atomic_write_json(intent_path, intent)
        current = self._instance(task_id, instance_id)
        current_process = current.get("process")
        reused_process = (
            isinstance(current_process, dict)
            and current_process.get("state") == "RUNNING"
        )
        if reused_process:
            launch = {
                "task_id": task_id,
                "instance_id": instance_id,
                "launch_id": current_process["launch_id"],
                "attempt_id": attempt_id,
                "state": "RUNNING",
                "host": self.supervisor.host,
                "port": current_process["port"],
                "pid": current_process["pid"],
                "started_at": current_process["started_at"],
                "code_identity": None,
            }
        else:
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
        progress.update(
            {
                "state": "AGENT_STARTING",
                "side_effect_stage": "PROCESS_PERSISTED",
                "launch_id": launch["launch_id"],
                "updated_at": utc_now(),
            }
        )
        atomic_write_json(intent_path, intent)
        if crash_hook:
            crash_hook(f"after_process_started:{instance_id}")
        recovery = None
        if previous_stage in {"AGENT_REQUESTED", "AGENT_ACCEPTED"}:
            recovery = adapter.recover(
                cast(AgentInstanceSnapshot, self._instance(task_id, instance_id))
            )
        if recovery is not None and recovery.recovered:
            adapter_result = {
                "accepted": True,
                "operation_id": attempt_id,
                "details": {"mode": "recovered", **deepcopy(recovery.details)},
            }
        else:
            progress.update(
                {
                    "side_effect_stage": "AGENT_REQUESTED",
                    "updated_at": utc_now(),
                }
            )
            atomic_write_json(intent_path, intent)
            started = adapter.start(instance_id, attempt_id)
            if not started.accepted:
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The Agent adapter rejected the prepared start operation.",
                    {"instance_id": instance_id},
                )
            adapter_result = {
                "accepted": True,
                "operation_id": started.operation_id,
                "details": {"mode": "started", **deepcopy(started.details)},
            }
        result = {
            "instance_id": instance_id,
            "launch": self._launch_summary(launch),
            "adapter": adapter_result,
        }
        progress.update(
            {
                "state": "RUNNING",
                "side_effect_stage": "AGENT_ACCEPTED",
                "last_error": None,
                "updated_at": utc_now(),
                "result": result,
            }
        )
        atomic_write_json(intent_path, intent)
        self.store.update_instance_fields(
            task_id,
            instance_id,
            {"start_failure": None},
            actor=Actor("system", "start_operation_runner"),
            command="clear_start_failure",
            idempotency_key=f"start-success-{intent['operation_id']}-{attempt}",
        )
        return result

    def _confirm_start_intent(self, intent_path: Path) -> None:
        intent = self._migrate_start_intent(intent_path, read_json(intent_path))
        if intent.get("confirmed_at") is not None:
            return
        task_id = intent["request"]["task_id"]
        plan = self._plan(task_id)
        if plan["task"]["status"] == "AWAITING_START_CONFIRMATION":
            self.commands.confirm_start(
                task_id,
                CommandEnvelope.model_validate(intent["request"]["envelope"]),
            )
        elif plan["task"]["status"] not in {"RUNNING", "BLOCKED_UNAVAILABLE", "FAILED"}:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A prepared start intent no longer belongs to an active task.",
                {"current": plan["task"]["status"]},
            )
        intent["confirmed_at"] = utc_now()
        intent["updated_at"] = utc_now()
        atomic_write_json(intent_path, intent)

    def _migrate_start_intent(
        self,
        intent_path: Path,
        intent: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        if intent.get("schema_version") == "1.1":
            return intent
        if intent.get("kind") != "START_READY_INSTANCES":
            return intent
        now = utc_now()
        migrated = deepcopy(intent)
        migrated.update(
            {
                "schema_version": "1.1",
                "state": "QUEUED" if intent.get("state") == "PREPARED" else intent["state"],
                "instance_progress": {
                    instance_id: {
                        "state": "PENDING",
                        "attempt": 0,
                        "launch_id": None,
                        "side_effect_stage": "NONE",
                        "last_error": None,
                        "updated_at": now,
                    }
                    for instance_id in intent.get("target_instance_ids", [])
                },
                "last_error": None,
                "created_at": intent.get("prepared_at", now),
                "updated_at": now,
                "completed_at": intent.get("committed_at"),
            }
        )
        if persist:
            atomic_write_json(intent_path, migrated)
        return migrated

    @staticmethod
    def _start_operation_summary(intent: dict[str, Any]) -> dict[str, Any]:
        progress = deepcopy(intent.get("instance_progress", {}))
        launches = [
            item.get("result")
            or {
                "instance_id": instance_id,
                "launch": {
                    "launch_id": item.get("launch_id"),
                    "state": item.get("state", "PENDING"),
                },
                "adapter": None,
            }
            for instance_id, item in progress.items()
        ]
        return {
            "schema_version": "1.1",
            "operation_id": intent["operation_id"],
            "task_id": intent["request"]["task_id"],
            "state": intent["state"],
            "instance_progress": progress,
            "last_error": deepcopy(intent.get("last_error")),
            "retry_allowed": intent["state"] == "RETRYABLE_FAILED",
            "unavailable": deepcopy(intent.get("unavailable", [])),
            "launches": launches,
            "created_at": intent.get("created_at") or intent.get("prepared_at"),
            "updated_at": intent.get("updated_at") or intent.get("prepared_at"),
            "completed_at": intent.get("completed_at"),
        }

    def _record_start_failure(
        self,
        intent_path: Path,
        intent: dict[str, Any],
        progress: dict[str, Any],
        instance_id: str,
        error: HarnessError,
    ) -> None:
        retryable = error.code in {"PROCESS_START_FAILED", "CONTROL_PLANE_NOT_READY"}
        phase = str(progress.get("state", "PREPARING"))
        public_details = {
            key: deepcopy(value)
            for key, value in error.details.items()
            if key
            in {
                "actual_sha256",
                "expected_sha256",
                "failure_type",
                "instance_id",
                "launch_id",
                "launch_state",
                "http_status",
                "route",
                "mismatches",
                "errors",
            }
        }
        failure = {
            "code": error.code,
            "message": error.message,
            "details": public_details,
            "phase": phase,
            "operation_id": intent["operation_id"],
            "attempt": int(progress.get("attempt", 1)),
            "retryable": retryable,
            "failed_at": utc_now(),
        }
        progress.update(
            {
                "state": "RETRYABLE_FAILED" if retryable else "ABORTED",
                "last_error": failure,
                "updated_at": failure["failed_at"],
            }
        )
        intent.update(
            {
                "state": "RETRYABLE_FAILED" if retryable else "ABORTED",
                "last_error": failure,
                "updated_at": failure["failed_at"],
            }
        )
        if not retryable:
            intent["completed_at"] = failure["failed_at"]
            intent["error"] = deepcopy(failure)
        atomic_write_json(intent_path, intent)
        current = self._instance(intent["request"]["task_id"], instance_id)
        if current["status"] in {"READY", "STARTING", "RUNNING"}:
            self.commands.transition_instance(
                current["task_id"],
                instance_id,
                "FAILED_TO_START",
                CommandEnvelope(
                    idempotency_key=(
                        f"start-failed-{intent['operation_id']}-{progress.get('attempt', 1)}"
                    ),
                    actor_type="system",
                    actor_id="start_operation_runner",
                    expected_revision=self.store.task.revision(
                        current["task_id"], current["task_id"]
                    ),
                ),
            )
        self.store.update_instance_fields(
            current["task_id"],
            instance_id,
            {"start_failure": failure},
            actor=Actor("system", "start_operation_runner"),
            command="record_start_failure",
            idempotency_key=(
                f"start-failure-detail-{intent['operation_id']}-{progress.get('attempt', 1)}"
            ),
        )

    def _restore_startable_instance(
        self, task_id: str, instance_id: str, operation_id: str, attempt: int
    ) -> None:
        instance = self._instance(task_id, instance_id)
        if instance["status"] != "FAILED_TO_START":
            return
        self.commands.transition_instance(
            task_id,
            instance_id,
            "READY",
            CommandEnvelope(
                idempotency_key=f"start-retry-ready-{operation_id}-{attempt}",
                actor_type="system",
                actor_id="start_operation_runner",
                expected_revision=self.store.task.revision(task_id, task_id),
            ),
        )

    def _prevalidate_plan(self, request: dict[str, Any]) -> None:
        self.commands.validate_plan_request(
            request["task_id"],
            stages=request["stages"],
            instances=request["instances"],
            task_cards=request["task_cards"],
            expected_revision=request["envelope"]["expected_revision"],
            mode=request.get("mode", "replace"),
            expected_plan_revision=request.get("expected_plan_revision"),
        )
        for card in request["task_cards"]:
            adapter = self.adapters.get_optional(card["agent_type"])
            if adapter is not None:
                self._require_valid_card(adapter, card)

    @staticmethod
    def _require_valid_card(adapter: AgentAdapter, card: TaskCard) -> None:
        validation = adapter.validate_task_card(card)
        if not validation.valid:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Agent adapter rejected its task card.",
                {"errors": list(validation.errors)},
            )
