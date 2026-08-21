"""Authoritative retry lineage resolution from Supervisor model-call attempts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError
from ..storage.atomic import digest_json, read_json
from ..storage.store import FileStateStore


@dataclass(frozen=True)
class RetryLineage:
    root_attempt_id: str
    root_instance_id: str
    retry_group_id: str


class RetryLineageAuthority:
    """Bind retries to registered roots and server-owned stable groups."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store

    def resolve(
        self,
        snapshot: dict[str, Any],
        task_id: str,
        instance_id: str,
        retry_of_attempt_id: str,
    ) -> RetryLineage:
        attempts_by_id = {
            item["attempt_id"]: item for item in snapshot["attempts"]
        }
        parent = attempts_by_id.get(retry_of_attempt_id)
        if parent is None:
            self._registered_root_attempt(task_id, instance_id, retry_of_attempt_id)
            root_attempt_id = retry_of_attempt_id
            root_instance_id = instance_id
        else:
            root_attempt_id, root_instance_id = self._lineage_root(
                attempts_by_id, parent
            )
            self._registered_root_attempt(task_id, root_instance_id, root_attempt_id)

        retry_group_id = self._derived_retry_group_id(task_id, root_attempt_id)
        if parent is not None and parent["retry_group_id"] != retry_group_id:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The registered retry lineage has an invalid retry group.",
            )
        return RetryLineage(root_attempt_id, root_instance_id, retry_group_id)

    def ensure_attempt_id_available(self, task_id: str, attempt_id: str) -> None:
        if self._attempt_path(task_id, attempt_id).exists():
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT",
                "The retry attempt id already belongs to a registered model call.",
            )

    @staticmethod
    def _lineage_root(
        attempts_by_id: dict[str, dict[str, Any]],
        parent: dict[str, Any],
    ) -> tuple[str, str]:
        current = parent
        visited: set[str] = set()
        while True:
            attempt_id = current["attempt_id"]
            if attempt_id in visited:
                raise HarnessError(
                    "VALIDATION_ERROR", "The registered retry lineage contains a cycle."
                )
            visited.add(attempt_id)
            root_attempt_id = current.get("root_attempt_id")
            root_instance_id = current.get("root_instance_id")
            if root_attempt_id is not None or root_instance_id is not None:
                if not isinstance(root_attempt_id, str) or not isinstance(
                    root_instance_id, str
                ):
                    raise HarnessError(
                        "VALIDATION_ERROR",
                        "The registered retry lineage has an invalid root binding.",
                    )
                return root_attempt_id, root_instance_id
            predecessor = attempts_by_id.get(current["retry_of_attempt_id"])
            if predecessor is None:
                return current["retry_of_attempt_id"], current["instance_id"]
            current = predecessor

    def _registered_root_attempt(
        self, task_id: str, instance_id: str, attempt_id: str
    ) -> dict[str, Any]:
        path = self._attempt_path(task_id, attempt_id)
        if not path.is_file():
            raise HarnessError(
                "VALIDATION_ERROR",
                "The retry root is not a registered model-call attempt.",
            )
        record = read_json(path)
        if not isinstance(record, dict) or (
            record.get("attempt_id") != attempt_id
            or record.get("task_id") != task_id
            or record.get("instance_id") != instance_id
        ):
            raise HarnessError(
                "VALIDATION_ERROR",
                "The retry root does not belong to the requested task and instance.",
            )
        return record

    def _attempt_path(self, task_id: str, attempt_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "tasks"
            / task_id
            / "attempts"
            / f"{attempt_id}.json"
        )

    @staticmethod
    def _derived_retry_group_id(task_id: str, root_attempt_id: str) -> str:
        digest = digest_json(
            {"task_id": task_id, "root_attempt_id": root_attempt_id}
        )
        return f"retry_group_{digest[:24]}"
