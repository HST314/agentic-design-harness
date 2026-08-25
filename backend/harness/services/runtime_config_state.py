"""Shared durable boundaries for runtime-configuration orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..storage.atomic import read_json
from ..storage.layout import validate_identifier
from ..storage.store import FileStateStore

_START_INTENT_KINDS = frozenset({"START_READY_INSTANCES", "START_INSTANCE", "RESTART_INSTANCE"})
_STARTED_STATUSES = frozenset(
    {
        "STARTING",
        "RUNNING",
        "WAITING_APPROVAL",
        "FAILED_TO_START",
        "SUCCEEDED",
        "FAILED",
        "CRASHED",
        "CANCELLED",
        "SUPERSEDED",
        "ARCHIVED",
    }
)


def application_task_lock_path(store: FileStateStore, task_id: str) -> Path:
    validate_identifier(task_id, "task_id")
    return store.layout.control_root / "locks" / f"application-task-{task_id}.lock"


def instance_has_launch_evidence(
    store: FileStateStore,
    task_id: str,
    instance_id: str,
    *,
    instance: dict[str, Any] | None = None,
) -> bool:
    """Use durable intents/records, never a resettable status alone, as truth."""

    validate_identifier(task_id, "task_id")
    validate_identifier(instance_id, "instance_id")
    selected = instance if instance is not None else store.instance.get(task_id, instance_id)
    if selected is None:
        return False
    if selected.get("process") is not None or selected.get("status") in _STARTED_STATUSES:
        return True
    launch_root = store.layout.control_root / "processes" / "launches"
    for path in launch_root.glob("*.json"):
        record = read_json(path)
        if record.get("task_id") == task_id and record.get("instance_id") == instance_id:
            return True
    intent_root = store.layout.control_root / "application-intents"
    for path in intent_root.glob("*.json"):
        intent = read_json(path)
        if intent.get("kind") not in _START_INTENT_KINDS:
            continue
        request = intent.get("request") or {}
        if request.get("task_id") != task_id:
            continue
        if intent.get("kind") == "START_READY_INSTANCES":
            if instance_id in intent.get("target_instance_ids", []):
                return True
        elif request.get("instance_id") == instance_id:
            return True
    return False


def instance_has_process_evidence(
    store: FileStateStore,
    task_id: str,
    instance_id: str,
    *,
    instance: dict[str, Any] | None = None,
) -> bool:
    """Return whether process creation crossed its durable side-effect boundary."""

    selected = instance if instance is not None else store.instance.get(task_id, instance_id)
    if selected is None:
        return False
    if selected.get("process") is not None:
        return True
    launch_root = store.layout.control_root / "processes" / "launches"
    return any(
        record.get("task_id") == task_id and record.get("instance_id") == instance_id
        for record in (read_json(path) for path in launch_root.glob("*.json"))
    )


def task_has_launch_evidence(store: FileStateStore, task_id: str) -> bool:
    plan = store.plan.get(task_id, task_id)
    instances = store.instance.list(task_id) if plan is None else plan["instances"]
    return any(
        instance_has_launch_evidence(
            store,
            task_id,
            str(instance["instance_id"]),
            instance=instance,
        )
        for instance in instances
    )


def is_unstarted_image_instance(
    store: FileStateStore,
    task_id: str,
    instance: dict[str, Any],
) -> bool:
    return bool(
        instance.get("task_id") == task_id
        and instance.get("agent_type") == "image"
        and not instance_has_launch_evidence(
            store,
            task_id,
            str(instance.get("instance_id", "")),
            instance=instance,
        )
    )
