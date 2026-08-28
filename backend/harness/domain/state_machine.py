"""Frozen transitions and deterministic Task/Stage aggregate state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError

EXECUTING = {"STARTING", "RUNNING"}
ACTIVE = {"READY", "STARTING", "RUNNING"}
WAITING = {"WAITING_APPROVAL"}
FAILED = {"FAILED_TO_START", "FAILED", "CRASHED"}
TASK_TERMINAL = {"SUCCEEDED", "PARTIAL", "CANCELLED"}


def stage_dependencies_authorized(
    stage: dict[str, Any],
    stage_by_id: dict[str, dict[str, Any]],
    instance_by_id: dict[str, dict[str, Any]],
) -> bool:
    """Return whether a stage may activate under delivery or the PPT human gate."""

    if all(stage_by_id[item]["status"] == "SUCCEEDED" for item in stage["depends_on"]):
        return True
    if stage["type"] != "ppt" or not stage["depends_on"]:
        return False
    image_instances = [
        item for item in instance_by_id.values() if item["agent_type"] == "image"
    ]
    return bool(image_instances) and all(
        item.get("manual_finished", False) for item in image_instances
    )


class StateMachine:
    def __init__(self, status_catalog_path: Path) -> None:
        self.catalog = json.loads(status_catalog_path.read_text(encoding="utf-8"))

    def transition(self, kind: str, current: str, target: str) -> None:
        transitions = self.catalog[kind]["transitions"]
        if target not in transitions.get(current, []):
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "The requested state transition is not allowed.",
                {"object_type": kind, "current": current, "target": target},
            )

    @staticmethod
    def aggregate_stage(
        stage: dict[str, Any],
        stage_by_id: dict[str, dict[str, Any]],
        instance_by_id: dict[str, dict[str, Any]],
    ) -> str:
        if stage["status"] in {"SKIPPED", "CANCELLED"}:
            return stage["status"]
        dependencies_authorized = stage_dependencies_authorized(
            stage, stage_by_id, instance_by_id
        )
        already_activated = (
            stage["requirement_lifecycle"]["first_activated_at"] is not None
        )
        if not dependencies_authorized and not already_activated:
            return "PENDING"
        instances = [instance_by_id[item] for item in stage["instance_ids"]]
        statuses = {item["status"] for item in instances}
        if statuses & EXECUTING:
            return "RUNNING"
        if "READY" in statuses or "CREATED" in statuses:
            # Once any sibling work has activated, remaining READY work keeps the
            # aggregate RUNNING. The frozen catalog intentionally has no
            # RUNNING -> READY regression.
            if stage["status"] in {"RUNNING", "WAITING_APPROVAL"}:
                return "RUNNING"
            return "READY"
        required = [item for item in instances if item["required"]]
        required_statuses = {item["status"] for item in required}
        if required_statuses & WAITING:
            return "WAITING_APPROVAL"
        if required_statuses & FAILED:
            return "FAILED"
        if "UNAVAILABLE" in required_statuses:
            return "UNAVAILABLE"
        if required and all(item["status"] in {"SUCCEEDED", "ARCHIVED"} for item in required):
            return "SUCCEEDED"
        if not required and stage["required"] is False:
            if any(item["status"] in {"SUCCEEDED", "ARCHIVED"} for item in instances):
                return "SUCCEEDED"
            return "SKIPPED"
        return stage["status"]

    @staticmethod
    def aggregate_task(
        task: dict[str, Any],
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        *,
        preserve_start_confirmation: bool = True,
    ) -> str:
        if task["status"] in TASK_TERMINAL:
            return task["status"]
        if preserve_start_confirmation and task["status"] == "AWAITING_START_CONFIRMATION":
            return task["status"]
        if any(item["status"] in ACTIVE for item in instances):
            return "RUNNING"
        if any(item["required"] and item["status"] == "WAITING_APPROVAL" for item in instances):
            return "WAITING_APPROVAL"
        if any(item["required"] and item["status"] in FAILED for item in instances):
            return "FAILED"
        if any(item["required"] and item["status"] == "UNAVAILABLE" for item in stages):
            return "BLOCKED_UNAVAILABLE"
        required_stages = [item for item in stages if item["required"]]
        complete = all(item["status"] == "SUCCEEDED" for item in required_stages)
        if not complete:
            return "RUNNING"
        downgraded = any(_is_activated_downgrade(item) for item in [*stages, *instances])
        return "PARTIAL" if downgraded else "SUCCEEDED"


def _is_activated_downgrade(item: dict[str, Any]) -> bool:
    lifecycle = item["requirement_lifecycle"]
    return (
        lifecycle["original_required"] is True
        and item["required"] is False
        and lifecycle["first_activated_at"] is not None
        and lifecycle["authorized_downgrade"] is not None
    )
