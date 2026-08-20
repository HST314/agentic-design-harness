"""Revisioned repositories on top of the event-first file commit protocol."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generic, TypeVar

from ..core.errors import HarnessError
from .atomic import atomic_write_json, digest_json, read_json
from .layout import StateLayout, validate_identifier
from .locks import FileLock
from .ndjson import append_record, recover_records

Payload = TypeVar("Payload", bound=dict[str, Any])
Validator = Callable[[dict[str, Any]], None]
CrashHook = Callable[[str], None]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class Actor:
    actor_type: str
    actor_id: str

    def as_dict(self) -> dict[str, str]:
        return {"actor_type": self.actor_type, "actor_id": self.actor_id}


class SnapshotRepository(Generic[Payload]):
    """Persist one contract object type as revisioned snapshots and full events."""

    def __init__(
        self,
        layout: StateLayout,
        object_type: str,
        relative_path: Callable[[str], Path],
        validator: Validator | None = None,
        lock_timeout_seconds: float = 5.0,
        after_commit: Callable[[str], None] | None = None,
    ) -> None:
        self.layout = layout
        self.object_type = object_type
        self._relative_path = relative_path
        self.validator = validator
        self.lock_timeout_seconds = lock_timeout_seconds
        self.after_commit = after_commit

    def path(self, task_id: str, object_id: str) -> Path:
        validate_identifier(task_id, "task_id")
        validate_identifier(object_id, f"{self.object_type}_id")
        return self.layout.control_root / "tasks" / task_id / self._relative_path(object_id)

    def read_wrapper(self, task_id: str, object_id: str) -> dict[str, Any] | None:
        path = self.path(task_id, object_id)
        if not path.exists():
            return None
        value = read_json(path)
        if not isinstance(value, dict) or value.get("object_type") != self.object_type:
            raise RuntimeError(f"invalid {self.object_type} snapshot wrapper")
        return value

    def get(self, task_id: str, object_id: str) -> Payload | None:
        wrapper = self.read_wrapper(task_id, object_id)
        return None if wrapper is None else wrapper["payload"]

    def revision(self, task_id: str, object_id: str) -> int:
        wrapper = self.read_wrapper(task_id, object_id)
        return 0 if wrapper is None else int(wrapper["revision"])

    def list(self, task_id: str) -> list[Payload]:
        directory = self.path(task_id, "placeholder").parent
        if not directory.exists():
            return []
        values: list[Payload] = []
        for path in sorted(directory.glob("*.json")):
            wrapper = read_json(path)
            if wrapper.get("object_type") == self.object_type:
                values.append(wrapper["payload"])
        return values

    def put(
        self,
        task_id: str,
        object_id: str,
        payload: Payload,
        *,
        expected_revision: int,
        actor: Actor,
        command: str,
        idempotency_key: str,
        command_result: dict[str, Any] | None = None,
        request_sha256: str | None = None,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        if (command_result is None) != (request_sha256 is None):
            raise ValueError("command_result and request_sha256 must be committed together")
        if self.validator:
            self.validator(payload)
        self.layout.initialize_task(task_id)
        lock = FileLock(
            self.layout.control_root / "locks" / f"task-{task_id}.lock",
            self.lock_timeout_seconds,
        )
        with lock:
            current = self.read_wrapper(task_id, object_id)
            actual_revision = 0 if current is None else int(current["revision"])
            if expected_revision != actual_revision:
                raise HarnessError(
                    "REVISION_CONFLICT",
                    "The object revision changed before this command committed.",
                    {
                        "object_type": self.object_type,
                        "object_id": object_id,
                        "expected_revision": expected_revision,
                        "actual_revision": actual_revision,
                    },
                )
            wrapper = {
                "store_version": "1.0",
                "object_type": self.object_type,
                "object_id": object_id,
                "revision": actual_revision + 1,
                "payload": payload,
                "committed_at": utc_now(),
            }
            event = {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "OBJECT_COMMITTED",
                "object_type": self.object_type,
                "object_id": object_id,
                "revision": wrapper["revision"],
                "payload_sha256": digest_json(payload),
                "actor": actor.as_dict(),
                "command": command,
                "idempotency_key": idempotency_key,
                "occurred_at": wrapper["committed_at"],
                "snapshot": wrapper,
            }
            if command_result is not None:
                event["command_result"] = command_result
                event["request_sha256"] = request_sha256
            event_path = self.layout.control_root / "tasks" / task_id / "events.ndjson"
            append_record(event_path, event)
            if crash_hook:
                crash_hook("after_event_append")
            atomic_write_json(self.path(task_id, object_id), wrapper)
            if crash_hook:
                crash_hook("after_snapshot_rename")
            if self.after_commit:
                self.after_commit(task_id)
            if crash_hook:
                crash_hook("after_index_update")
            return wrapper


class TaskRepository(SnapshotRepository[dict[str, Any]]):
    pass


class PlanRepository(SnapshotRepository[dict[str, Any]]):
    pass


class StageRepository(SnapshotRepository[dict[str, Any]]):
    pass


class InstanceRepository(SnapshotRepository[dict[str, Any]]):
    pass


class ApprovalRepository(SnapshotRepository[dict[str, Any]]):
    pass


class InboxRepository(SnapshotRepository[dict[str, Any]]):
    pass


class RetryBudgetRepository(SnapshotRepository[dict[str, Any]]):
    pass


class UsageRepository:
    def __init__(
        self,
        layout: StateLayout,
        validator: Validator,
        lock_timeout_seconds: float,
    ) -> None:
        self.layout = layout
        self.validator = validator
        self.lock_timeout_seconds = lock_timeout_seconds

    def append(self, task_id: str, payload: dict[str, Any]) -> bool:
        self.validator(payload)
        self.layout.initialize_task(task_id)
        path = self.layout.control_root / "tasks" / task_id / "usage.ndjson"
        with FileLock(
            self.layout.control_root / "locks" / f"task-{task_id}.lock",
            self.lock_timeout_seconds,
        ):
            existing = recover_records(path)
            if any(item.get("event_id") == payload["event_id"] for item in existing):
                return False
            append_record(path, payload)
        return True

    def list(self, task_id: str) -> list[dict[str, Any]]:
        path = self.layout.control_root / "tasks" / task_id / "usage.ndjson"
        return recover_records(path)
