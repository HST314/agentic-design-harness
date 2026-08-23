"""Plan persistence and startup recovery application use cases."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ..adapters import PrepareRequest
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import atomic_write_json, read_json
from ..storage.repository import utc_now

if TYPE_CHECKING:
    from ..adapters import AgentAdapter, TaskCard

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
        self.commands.validate_plan_request(
            request["task_id"],
            stages=request["stages"],
            instances=request["instances"],
            task_cards=request["task_cards"],
            expected_revision=request["envelope"]["expected_revision"],
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
