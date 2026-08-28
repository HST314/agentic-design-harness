"""Read-only WorkItem projections shared by board, plan and detail views."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from ..adapters import AdapterRegistry
from ..core.errors import HarnessError
from ..storage.atomic import digest_json
from ..storage.layout import validate_identifier
from ..storage.store import FileStateStore
from .approvals import ApprovalInboxService
from .assets import AssetService
from .retry_budget import RetryBudgetService

_ACTIVE_STATUSES = {"STARTING", "RUNNING"}
_TODO_STATUSES = {"CREATED", "PENDING", "READY", "AWAITING_START_CONFIRMATION"}
_COMPLETE_STATUSES = {"SUCCEEDED", "SKIPPED", "SUPERSEDED", "ARCHIVED"}


def logical_work_items(
    store: FileStateStore,
    task_id: str,
    plan: dict[str, Any],
    stages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve the canonical WorkItem identities for one saved plan."""

    confirmed = [
        item
        for item in store.plan_proposal.list(task_id)
        if item["status"] == "CONFIRMED"
        and item["revision"] == plan["task"]["plan_revision"]
    ]
    if confirmed:
        latest = max(confirmed, key=lambda item: (item["revision"], item["updated_at"]))
        return deepcopy(latest["work_items"])

    # API-created legacy plans predate WorkItem. Each immutable execution card
    # receives a deterministic logical identity so retries never duplicate it.
    raw_items: list[dict[str, Any]] = []
    for card in plan["task_cards"]:
        identifier = hashlib.sha256(card["card_id"].encode("utf-8")).hexdigest()[:24]
        raw_items.append(
            {
                "schema_version": "1.0",
                "work_item_id": f"work_{identifier}",
                "task_id": task_id,
                "stage_id": card["stage_id"],
                "title": card["objective"],
                "agent_type": card["agent_type"],
                "required": next(
                    item["required"]
                    for item in plan["instances"]
                    if item["instance_id"] == card["instance_id"]
                ),
                "depends_on": [],
                "current_instance_id": card["instance_id"],
                "instance_ids": [card["instance_id"]],
                "task_card_ids": [card["card_id"]],
            }
        )
    items_by_stage: dict[str, list[str]] = {}
    for item in raw_items:
        items_by_stage.setdefault(item["stage_id"], []).append(item["work_item_id"])
    stage_by_id = {item["stage_id"]: item for item in stages}
    for item in raw_items:
        stage = stage_by_id[item["stage_id"]]
        item["depends_on"] = [
            dependency_item
            for dependency_stage in stage["depends_on"]
            for dependency_item in items_by_stage.get(dependency_stage, [])
        ]
    return raw_items


class WorkItemProjectionService:
    """Build a stable logical-card read model without writing domain state."""

    def __init__(
        self,
        store: FileStateStore,
        approvals: ApprovalInboxService,
        assets: AssetService,
        retry_budgets: RetryBudgetService,
        adapters: AdapterRegistry,
    ) -> None:
        self.store = store
        self.approvals = approvals
        self.assets = assets
        self.retry_budgets = retry_budgets
        self.adapters = adapters

    def list(self, task_id: str) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        task = self.store.task.get(task_id, task_id)
        if task is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            return self._response(task, [], [], self.store.task.revision(task_id, task_id))

        stages = sorted(plan["stages"], key=lambda item: item["position"])
        stage_by_id = {item["stage_id"]: item for item in stages}
        instance_by_id = {item["instance_id"]: item for item in plan["instances"]}
        logical_items = self._logical_items(task_id, plan, stages)
        approvals = self.approvals.list_approvals(task_id=task_id, status="PENDING")
        retry_snapshot = self.retry_budgets.get(task_id)
        assets = self.assets.list_assets(task_id)
        projected = [
            self._project_item(
                task,
                item,
                stage_by_id,
                instance_by_id,
                approvals,
                retry_snapshot["attempts"],
                assets,
            )
            for item in logical_items
        ]
        stage_views = self._stage_views(stages, projected)
        plan_revision = self.store.plan.revision(task_id, task_id)
        return self._response(task, stage_views, projected, plan_revision)

    def get(self, task_id: str, work_item_id: str) -> dict[str, Any]:
        validate_identifier(work_item_id, "work_item_id")
        response = self.list(task_id)
        item = next(
            (entry for entry in response["items"] if entry["work_item_id"] == work_item_id),
            None,
        )
        if item is None:
            raise HarnessError(
                "TASK_NOT_FOUND",
                "The requested WorkItem does not exist in the selected task.",
                {"task_id": task_id, "work_item_id": work_item_id},
            )
        return {
            "schema_version": "1.0",
            "task": response["task"],
            "projection_revision": response["projection_revision"],
            "refresh_after_ms": response["refresh_after_ms"],
            "item": item,
        }

    def _logical_items(
        self,
        task_id: str,
        plan: dict[str, Any],
        stages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return logical_work_items(self.store, task_id, plan, stages)

    def _project_item(
        self,
        task: dict[str, Any],
        item: dict[str, Any],
        stage_by_id: dict[str, dict[str, Any]],
        instance_by_id: dict[str, dict[str, Any]],
        approvals: list[dict[str, Any]],
        attempts: list[dict[str, Any]],
        assets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        stage = stage_by_id.get(item["stage_id"])
        if stage is None:
            raise HarnessError(
                "INTERNAL_ERROR",
                "A WorkItem projection references a missing Stage.",
                {"work_item_id": item["work_item_id"], "stage_id": item["stage_id"]},
            )
        item_instances = [
            instance_by_id[instance_id]
            for instance_id in item["instance_ids"]
            if instance_id in instance_by_id
        ]
        current_instance_id = item.get("current_instance_id")
        current = (
            instance_by_id.get(current_instance_id)
            if isinstance(current_instance_id, str)
            else None
        )
        if current is None and item_instances:
            current = item_instances[-1]
        item_approvals = [
            approval
            for approval in approvals
            if approval["instance_id"] in item["instance_ids"]
        ]
        item_attempts = [
            attempt
            for attempt in attempts
            if attempt["instance_id"] in item["instance_ids"]
        ]
        raw_status, business_status = self._business_status(
            stage["status"], item_instances, current, item_approvals
        )
        available = bool(
            (adapter := self.adapters.get_optional(item["agent_type"]))
            and adapter.available
        )
        delivery_count = sum(
            1
            for asset in assets
            if asset["manifest"].get("producer_instance_id") in item["instance_ids"]
            and asset["integrity_status"] == "VERIFIED"
        )
        alerts = self._alerts(item, current, item_approvals, available)
        timestamps = [task["updated_at"]]
        timestamps.extend(approval["created_at"] for approval in item_approvals)
        timestamps.extend(attempt["created_at"] for attempt in item_attempts)
        timestamps.extend(
            asset["manifest"].get("published_at", asset["manifest"]["created_at"])
            for asset in assets
            if asset["manifest"].get("producer_instance_id") in item["instance_ids"]
        )
        projection = {
            "schema_version": "1.0",
            "work_item_id": item["work_item_id"],
            "task_id": item["task_id"],
            "title": item["title"],
            "agent_type": item["agent_type"],
            "required": item["required"],
            "depends_on": deepcopy(item["depends_on"]),
            "stage": {
                "stage_id": stage["stage_id"],
                "position": stage["position"],
                "type": stage["type"],
                "status": stage["status"],
                "depends_on": deepcopy(stage["depends_on"]),
                "available": available,
            },
            "business_status": business_status,
            "raw_status": raw_status,
            "current_instance": self._instance_summary(current),
            "instance_ids": deepcopy(item["instance_ids"]),
            "attempts": [
                {
                    "attempt_id": attempt["attempt_id"],
                    "instance_id": attempt["instance_id"],
                    "status": attempt["status"],
                    "automatic": attempt["automatic"],
                    "created_at": attempt["created_at"],
                    "settled_at": attempt.get("settled_at"),
                }
                for attempt in item_attempts
            ],
            "pending_approvals": [
                {
                    "approval_id": approval["approval_id"],
                    "kind": approval["kind"],
                    "owner": approval["owner"],
                    "created_at": approval["created_at"],
                }
                for approval in item_approvals
            ],
            "delivery_count": delivery_count,
            "alerts": alerts,
            "updated_at": max(timestamps),
        }
        self.store.contracts.validate("work-item-projection", projection)
        return projection

    @staticmethod
    def _business_status(
        stage_status: str,
        instances: list[dict[str, Any]],
        current: dict[str, Any] | None,
        approvals: list[dict[str, Any]],
    ) -> tuple[str, str]:
        active = next(
            (item["status"] for item in instances if item["status"] in _ACTIVE_STATUSES),
            None,
        )
        if active is not None:
            return active, "RUNNING"
        waiting = next(
            (item["status"] for item in instances if item["status"] == "WAITING_APPROVAL"),
            None,
        )
        if waiting is not None or approvals:
            return waiting or "WAITING_APPROVAL", "WAITING_APPROVAL"
        raw = current["status"] if current is not None else stage_status
        if raw in _TODO_STATUSES:
            return raw, "TODO"
        if raw in _COMPLETE_STATUSES:
            return raw, "COMPLETED"
        return raw, "EXCEPTION"

    @staticmethod
    def _instance_summary(instance: dict[str, Any] | None) -> dict[str, Any] | None:
        if instance is None:
            return None
        process = instance.get("process")
        return {
            "instance_id": instance["instance_id"],
            "status": instance["status"],
            "approval_mode": instance["approval_mode"],
            "manual_finished": bool(instance.get("manual_finished", False)),
            "process_state": None if process is None else process["state"],
            "restart_required": bool(instance.get("restart_required", False)),
            "created_at": instance["created_at"],
        }

    @staticmethod
    def _alerts(
        item: dict[str, Any],
        current: dict[str, Any] | None,
        approvals: list[dict[str, Any]],
        available: bool,
    ) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []
        if not available:
            alerts.append(
                {
                    "code": "ADAPTER_UNAVAILABLE",
                    "severity": "error",
                    "message": (
                        f"{item['agent_type'].upper()} 能力当前不可用。"
                        "请检查对应 Agent 的锁定依赖和运行配置。"
                    ),
                }
            )
        if current is None:
            alerts.append(
                {
                    "code": "INSTANCE_MISSING",
                    "severity": "error",
                    "message": "当前运行实例不存在, 请检查计划投影。",
                }
            )
        elif current.get("start_failure"):
            failure = current["start_failure"]
            alerts.append(
                {
                    "code": str(failure["code"]),
                    "severity": "error",
                    "message": str(failure["message"]),
                }
            )
        elif current.get("delivery_rejection"):
            rejection = current["delivery_rejection"]
            alerts.append(
                {
                    "code": str(rejection["code"]),
                    "severity": "error",
                    "message": str(rejection["message"]),
                }
            )
        if approvals:
            alerts.append(
                {
                    "code": "APPROVAL_REQUIRED",
                    "severity": "warning",
                    "message": f"有 {len(approvals)} 项人工或 Master 审批待处理。",
                }
            )
        return alerts

    def _stage_views(
        self,
        stages: list[dict[str, Any]],
        items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "stage_id": stage["stage_id"],
                "position": stage["position"],
                "type": stage["type"],
                "required": stage["required"],
                "depends_on": deepcopy(stage["depends_on"]),
                "status": stage["status"],
                "available": bool(
                    (adapter := self.adapters.get_optional(stage["type"]))
                    and adapter.available
                ),
                "work_item_ids": [
                    item["work_item_id"]
                    for item in items
                    if item["stage"]["stage_id"] == stage["stage_id"]
                ],
            }
            for stage in stages
        ]

    @staticmethod
    def _response(
        task: dict[str, Any],
        stages: list[dict[str, Any]],
        items: list[dict[str, Any]],
        plan_revision: int,
    ) -> dict[str, Any]:
        summary = {
            status: sum(item["business_status"] == status for item in items)
            for status in ("TODO", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "EXCEPTION")
        }
        response = {
            "schema_version": "1.0",
            "task": deepcopy(task),
            "stages": stages,
            "items": items,
            "summary": summary,
            "refresh_after_ms": 3_000 if summary["RUNNING"] else 5_000,
        }
        response["projection_revision"] = digest_json(
            {
                "task_revision": task["plan_revision"],
                "plan_store_revision": plan_revision,
                "stages": stages,
                "items": items,
            }
        )
        return response
