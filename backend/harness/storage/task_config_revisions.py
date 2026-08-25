"""Versioned main-task configuration storage with legacy snapshot reads."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError
from .atomic import atomic_write_bytes, atomic_write_json, canonical_json_bytes, digest_json
from .config_revision_io import (
    CrashHook,
    ensure_private_directory,
    invoke_crash_hook,
    read_json_object,
    read_regular_bytes,
    recover_temporary_paths,
    validate_public_config_tree,
)
from .layout import validate_identifier
from .locks import FileLock
from .store import FileStateStore

_LEGACY_REQUIRED = {
    "schema_version",
    "task_id",
    "source_config_revision",
    "config_hash",
    "providers",
    "model_list",
    "runtime",
    "created_at",
}


class TaskConfigRevisionStore:
    """Commit immutable task baselines before atomically advancing their state."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store

    def commit(
        self,
        task_id: str,
        revision: dict[str, Any],
        state: dict[str, Any],
        *,
        expected_revision: int,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        self.store.contracts.validate("task-config-revision", revision)
        self.store.contracts.validate("task-config-state", state)
        self._validate_pair(task_id, revision, state)
        with FileLock(self._lock_path(task_id), self.store.lock_timeout_seconds):
            current = self.read_current(task_id)
            current_number = 0 if current is None else int(current["state"]["revision"])
            if current_number != expected_revision:
                self._conflict(expected_revision, current_number)
            if state["revision"] != expected_revision + 1:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The next task configuration state revision is not consecutive.",
                    {"expected_revision": expected_revision + 1},
                )
            expected_parent = None if current is None else current["revision"]["revision_id"]
            if revision["parent_revision_id"] != expected_parent:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The task configuration revision parent is stale.",
                    {"expected_parent_revision_id": expected_parent},
                )
            next_sequence = (
                1 if expected_parent is None else self._revision_sequence(expected_parent) + 1
            )
            expected_revision_id = f"task-config-r{next_sequence:06d}"
            if revision["revision_id"] != expected_revision_id:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The next task configuration revision ID is not consecutive.",
                    {"expected_revision_id": expected_revision_id},
                )
            self._write_immutable_revision(task_id, revision)
            invoke_crash_hook(crash_hook, "after_task_revision_published")
            atomic_write_json(self._state_path(task_id), state, mode=0o640)
            invoke_crash_hook(crash_hook, "after_task_state_published")
            return {"state": deepcopy(state), "revision": deepcopy(revision), "legacy": False}

    def read_current(self, task_id: str) -> dict[str, Any] | None:
        validate_identifier(task_id, "task_id")
        state_path = self._state_path(task_id)
        if not state_path.exists():
            legacy = self._legacy_pair(task_id)
            return deepcopy(legacy) if legacy is not None else None
        state = read_json_object(state_path, trusted_root=self._config_root(task_id))
        self._validate_persisted("task-config-state", state)
        validate_public_config_tree(state)
        if state.get("task_id") != task_id:
            self._integrity("The task configuration state belongs to another task.")
        revision = self.read_revision(task_id, str(state["current_revision_id"]))
        if state["source_system_revision"] != revision["source_system_revision"]:
            self._integrity("The task configuration state source revision is inconsistent.")
        return {"state": state, "revision": revision, "legacy": False}

    def read_revision(self, task_id: str, revision_id: str) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        path = self._revision_path(task_id, revision_id)
        if not path.exists() and revision_id == "task-config-r000001":
            legacy = self._legacy_pair(task_id)
            if legacy is not None:
                return deepcopy(legacy["revision"])
        document = read_json_object(path, trusted_root=self._revisions_root(task_id))
        self._validate_persisted("task-config-revision", document)
        if document.get("task_id") != task_id or document.get("revision_id") != revision_id:
            self._integrity("The task configuration revision identity is inconsistent.")
        if document.get("parent_revision_id") == revision_id:
            self._integrity("A task configuration revision cannot be its own parent.")
        self._validate_revision_hash(document)
        return document

    def recover(self, task_id: str) -> dict[str, Any]:
        """Remove abandoned atomic-write artifacts without promoting orphan revisions."""

        validate_identifier(task_id, "task_id")
        removed = [
            *recover_temporary_paths(self._config_root(task_id)),
            *recover_temporary_paths(self._revisions_root(task_id)),
        ]
        current = self.read_current(task_id)
        active = None if current is None else current["revision"]["revision_id"]
        referenced: set[str] = set()
        cursor = None if current is None else current["revision"]
        while cursor is not None:
            revision_id = str(cursor["revision_id"])
            if revision_id in referenced:
                self._integrity("The task configuration revision ancestry contains a cycle.")
            referenced.add(revision_id)
            parent = cursor["parent_revision_id"]
            cursor = None if parent is None else self.read_revision(task_id, str(parent))
        revisions_root = self._revisions_root(task_id)
        present = sorted(
            path.stem
            for path in revisions_root.glob("task-config-r*.json")
            if path.is_file()
        )
        return {
            "removed_temporary_paths": removed,
            "current_revision_id": active,
            "unreferenced_revision_ids": [item for item in present if item not in referenced],
        }

    @staticmethod
    def build_revision(
        *,
        task_id: str,
        revision_id: str,
        parent_revision_id: str | None,
        source_system_revision: str,
        provider_ids: list[str],
        model_list: dict[str, Any],
        runtime: dict[str, Any],
        created_by: dict[str, str],
        created_at: str,
    ) -> dict[str, Any]:
        body = {
            "provider_ids": sorted(provider_ids),
            "model_list": deepcopy(model_list),
            "runtime": deepcopy(runtime),
        }
        return {
            "schema_version": "2.0",
            "task_id": task_id,
            "revision_id": revision_id,
            "parent_revision_id": parent_revision_id,
            "source_system_revision": source_system_revision,
            **body,
            "config_hash": digest_json(body),
            "created_by": dict(created_by),
            "created_at": created_at,
        }

    def _write_immutable_revision(self, task_id: str, revision: dict[str, Any]) -> None:
        ensure_private_directory(self._config_root(task_id))
        root = self._revisions_root(task_id)
        ensure_private_directory(root)
        path = self._revision_path(task_id, revision["revision_id"])
        content = canonical_json_bytes(revision) + b"\n"
        if path.exists():
            actual = read_regular_bytes(path, trusted_root=root)
            if actual != content:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "An immutable task configuration revision ID was reused.",
                    {"revision_id": revision["revision_id"]},
                )
            return
        atomic_write_bytes(path, content, mode=0o440)

    def _legacy_pair(self, task_id: str) -> dict[str, Any] | None:
        path = self._legacy_path(task_id)
        if not path.exists():
            return None
        legacy = load_legacy_task_snapshot(path)
        if legacy["task_id"] != task_id:
            self._integrity("The legacy task configuration belongs to another task.")
        revision = self.build_revision(
            task_id=task_id,
            revision_id="task-config-r000001",
            parent_revision_id=None,
            source_system_revision=legacy["source_config_revision"],
            provider_ids=sorted(legacy["providers"]),
            model_list=legacy["model_list"],
            runtime=legacy["runtime"],
            created_by={"type": "system", "id": "legacy_migration"},
            created_at=legacy["created_at"],
        )
        state = {
            "schema_version": "2.0",
            "task_id": task_id,
            "current_revision_id": revision["revision_id"],
            "source_system_revision": revision["source_system_revision"],
            "locked_at": None,
            "locked_reason": None,
            "revision": 1,
            "created_at": legacy["created_at"],
            "updated_at": legacy["created_at"],
        }
        self._validate_persisted("task-config-revision", revision)
        self._validate_persisted("task-config-state", state)
        return {"state": state, "revision": revision, "legacy": True}

    def _validate_pair(
        self, task_id: str, revision: dict[str, Any], state: dict[str, Any]
    ) -> None:
        validate_public_config_tree(state)
        if revision["task_id"] != task_id or state["task_id"] != task_id:
            raise HarnessError("VALIDATION_ERROR", "Configuration records belong to another task.")
        if state["current_revision_id"] != revision["revision_id"]:
            raise HarnessError(
                "VALIDATION_ERROR", "The task configuration state points to another revision."
            )
        if state["source_system_revision"] != revision["source_system_revision"]:
            raise HarnessError(
                "VALIDATION_ERROR", "The task configuration source revision is inconsistent."
            )
        if revision["parent_revision_id"] == revision["revision_id"]:
            raise HarnessError(
                "VALIDATION_ERROR", "A task configuration revision cannot be its own parent."
            )
        self._validate_revision_hash(revision)

    @staticmethod
    def _validate_revision_hash(revision: dict[str, Any]) -> None:
        validate_public_config_tree(revision)
        body = {
            "provider_ids": revision["provider_ids"],
            "model_list": revision["model_list"],
            "runtime": revision["runtime"],
        }
        if digest_json(body) != revision["config_hash"]:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The task configuration revision failed its content hash check.",
                {"revision_id": revision["revision_id"]},
            )

    def _validate_persisted(self, schema: str, document: dict[str, Any]) -> None:
        try:
            self.store.contracts.validate(schema, document)
        except HarnessError as exc:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "A persisted task configuration document failed contract validation.",
                {"schema": schema},
            ) from exc

    def _config_root(self, task_id: str) -> Path:
        return self.store.layout.control_root / "tasks" / task_id / "master" / "config"

    def _revisions_root(self, task_id: str) -> Path:
        return self._config_root(task_id) / "revisions"

    def _state_path(self, task_id: str) -> Path:
        return self._config_root(task_id) / "state.json"

    def _revision_path(self, task_id: str, revision_id: str) -> Path:
        validate_identifier(revision_id, "revision_id")
        return self._revisions_root(task_id) / f"{revision_id}.json"

    def _legacy_path(self, task_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "tasks"
            / task_id
            / "master"
            / "config-snapshot.json"
        )

    def _lock_path(self, task_id: str) -> Path:
        return self.store.layout.control_root / "locks" / f"task-config-{task_id}.lock"

    @staticmethod
    def _conflict(expected: int, actual: int) -> None:
        raise HarnessError(
            "SETTINGS_REVISION_CONFLICT",
            "The task configuration state changed after it was read.",
            {"expected_revision": expected, "actual_revision": actual},
        )

    @staticmethod
    def _revision_sequence(revision_id: str) -> int:
        return int(revision_id.rsplit("r", 1)[1])

    @staticmethod
    def _integrity(message: str) -> None:
        raise HarnessError("CONFIG_INTEGRITY_FAILED", message)


def load_legacy_task_snapshot(path: Path) -> dict[str, Any]:
    """Read the frozen 1.0 snapshot without rewriting or weakening its hash."""

    document = read_json_object(path, trusted_root=path.parent)
    if (
        set(document) != _LEGACY_REQUIRED
        or document.get("schema_version") != "1.0"
        or not isinstance(document.get("providers"), dict)
        or any(
            not isinstance(value, dict)
            or set(value) != {"base_url"}
            or not isinstance(value.get("base_url"), str)
            for value in document["providers"].values()
        )
    ):
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED", "The legacy task configuration snapshot is invalid."
        )
    body = {
        "providers": document["providers"],
        "model_list": document["model_list"],
        "runtime": document["runtime"],
    }
    actual_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if actual_hash != document["config_hash"]:
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "The legacy task configuration snapshot failed its content hash check.",
        )
    return deepcopy(document)
