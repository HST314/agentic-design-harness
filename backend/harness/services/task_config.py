"""Immutable, secret-free task snapshots derived from deployment configuration."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..core.config_kernel import (
    ConfigSnapshot,
    ModelListConfig,
    ProviderConfig,
    RuntimeConfig,
)
from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.store import FileStateStore


class TaskConfigService:
    """Pin non-secret runtime choices once while resolving secrets only in memory."""

    def __init__(self, store: FileStateStore, process_snapshot: ConfigSnapshot | None) -> None:
        self.store = store
        self.process_snapshot = process_snapshot

    def pin(self, task_id: str) -> dict[str, Any]:
        """Pin the current configuration for an already durable task."""

        return self._pin(task_id, require_task=True)

    def pin_for_creation(self, task_id: str) -> dict[str, Any]:
        """Pin before task creation so a process crash cannot shift revisions."""

        return self._pin(task_id, require_task=False)

    def _pin(self, task_id: str, *, require_task: bool) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        if require_task and self.store.task.get(task_id, task_id) is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        path = self._path(task_id)
        with FileLock(self._lock_path(task_id), self.store.lock_timeout_seconds):
            if path.exists():
                return self._load_document(path)
            snapshot = self._require_process_snapshot()
            body = {
                "providers": {
                    name: {"base_url": provider.base_url}
                    for name, provider in snapshot.providers.providers.items()
                },
                "model_list": snapshot.model_list.model_dump(mode="json"),
                "runtime": snapshot.runtime.model_dump(mode="json"),
            }
            config_hash = hashlib.sha256(
                json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            document = {
                "schema_version": "1.0",
                "task_id": task_id,
                "source_config_revision": snapshot.revision,
                "config_hash": config_hash,
                **body,
                "created_at": utc_now(),
            }
            atomic_write_json(path, document, mode=0o640)
            return deepcopy(document)

    def resolve(self, task_id: str) -> ConfigSnapshot:
        document = self.pin(task_id)
        current = self._require_process_snapshot()
        raw_providers: dict[str, Any] = {}
        for name, public_provider in document["providers"].items():
            current_provider = current.providers.providers.get(name)
            if current_provider is None:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "The task configuration references an unavailable provider.",
                    {"provider": name},
                )
            raw_providers[name] = {
                "base_url": public_provider["base_url"],
                "api_key": current_provider.api_key,
            }
        return ConfigSnapshot(
            schema_version="1.0",
            revision=document["source_config_revision"],
            providers=ProviderConfig(
                schema_version="1.0",
                providers=raw_providers,
            ),
            model_list=ModelListConfig.model_validate(document["model_list"]),
            runtime=RuntimeConfig.model_validate(document["runtime"]),
        )

    def get_public(self, task_id: str) -> dict[str, Any]:
        path = self._path(task_id)
        if not path.exists():
            return self.pin(task_id)
        return self._load_document(path)

    def source_citations_required(self, task_id: str) -> bool:
        """Return the immutable task policy used to validate Master plans."""

        document = self.get_public(task_id)
        return bool(document["runtime"]["document_processing"]["require_source_citations"])

    def _require_process_snapshot(self) -> ConfigSnapshot:
        if self.process_snapshot is None:
            raise HarnessError(
                "VALIDATION_ERROR",
                "A validated deployment configuration is required for Master execution.",
            )
        return self.process_snapshot

    def _path(self, task_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "tasks"
            / task_id
            / "master"
            / "config-snapshot.json"
        )

    def _lock_path(self, task_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "locks"
            / f"task-config-{task_id}.lock"
        )

    @staticmethod
    def _load_document(path: Path) -> dict[str, Any]:
        document = read_json(path)
        required = {
            "schema_version",
            "task_id",
            "source_config_revision",
            "config_hash",
            "providers",
            "model_list",
            "runtime",
            "created_at",
        }
        if (
            not isinstance(document, dict)
            or set(document) != required
            or document.get("schema_version") != "1.0"
            or not isinstance(document.get("providers"), dict)
            or any(
                not isinstance(value, dict)
                or set(value) != {"base_url"}
                or not isinstance(value.get("base_url"), str)
                for value in document["providers"].values()
            )
        ):
            raise HarnessError("VALIDATION_ERROR", "The task configuration snapshot is invalid.")
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
                "VALIDATION_ERROR", "The task configuration snapshot failed integrity checks."
            )
        return deepcopy(document)
