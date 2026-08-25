"""Versioned, secret-free task configuration baselines."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..core.config_kernel import (
    ConfigSnapshot,
    ModelListConfig,
    ProviderConfig,
    RuntimeConfig,
)
from ..core.errors import HarnessError
from ..storage.layout import validate_identifier
from ..storage.repository import utc_now
from ..storage.store import FileStateStore
from ..storage.task_config_revisions import (
    TaskConfigRevisionStore,
    load_legacy_task_snapshot,
)


class TaskConfigService:
    """Pin deployment defaults as immutable task revisions.

    Provider credentials and service URLs remain process-only.  Durable task
    revisions contain Provider identifiers, approved model metadata, and safe
    runtime values; secret resolution happens only when a caller needs an
    in-memory ``ConfigSnapshot``.
    """

    def __init__(self, store: FileStateStore, process_snapshot: ConfigSnapshot | None) -> None:
        self.store = store
        self.process_snapshot = process_snapshot
        self.revisions = TaskConfigRevisionStore(store)

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
        current = self.revisions.read_current(task_id)
        if current is not None:
            return self._public_document(task_id, current)
        snapshot = self.require_process_snapshot()
        now = utc_now()
        revision = self.revisions.build_revision(
            task_id=task_id,
            revision_id="task-config-r000001",
            parent_revision_id=None,
            source_system_revision=snapshot.revision,
            provider_ids=sorted(snapshot.providers.providers),
            model_list=snapshot.model_list.model_dump(mode="json"),
            runtime=snapshot.runtime.model_dump(mode="json"),
            created_by={"type": "system", "id": "task_config_service"},
            created_at=now,
        )
        state = {
            "schema_version": "2.0",
            "task_id": task_id,
            "current_revision_id": revision["revision_id"],
            "source_system_revision": revision["source_system_revision"],
            "locked_at": None,
            "locked_reason": None,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
        }
        try:
            committed = self.revisions.commit(
                task_id,
                revision,
                state,
                expected_revision=0,
            )
        except HarnessError as exc:
            if exc.code != "SETTINGS_REVISION_CONFLICT":
                raise
            committed = self.revisions.read_current(task_id)
            if committed is None:
                raise
        return self._public_document(task_id, committed)

    def rebase(
        self,
        task_id: str,
        *,
        created_by: dict[str, str],
    ) -> dict[str, Any]:
        """Advance one unlocked task baseline to the current system revision.

        Callers must hold the application task lock and re-check durable launch
        evidence before calling this method.
        """

        current = self.revisions.read_current(task_id)
        if current is None:
            self.pin(task_id)
            current = self.revisions.read_current(task_id)
        assert current is not None
        if current["state"]["locked_at"] is not None:
            raise HarnessError(
                "INSTANCE_CONFIG_LOCKED",
                "The task configuration baseline was locked by an accepted start.",
                {"task_id": task_id},
            )
        snapshot = self.require_process_snapshot()
        if current["revision"]["source_system_revision"] == snapshot.revision:
            return self._public_document(task_id, current)
        sequence = int(str(current["revision"]["revision_id"]).rsplit("r", 1)[1]) + 1
        now = utc_now()
        revision = self.revisions.build_revision(
            task_id=task_id,
            revision_id=f"task-config-r{sequence:06d}",
            parent_revision_id=current["revision"]["revision_id"],
            source_system_revision=snapshot.revision,
            provider_ids=sorted(snapshot.providers.providers),
            model_list=snapshot.model_list.model_dump(mode="json"),
            runtime=snapshot.runtime.model_dump(mode="json"),
            created_by=created_by,
            created_at=now,
        )
        state = {
            **current["state"],
            "current_revision_id": revision["revision_id"],
            "source_system_revision": revision["source_system_revision"],
            "revision": int(current["state"]["revision"]) + 1,
            "updated_at": now,
        }
        committed = self.revisions.commit(
            task_id,
            revision,
            state,
            expected_revision=int(current["state"]["revision"]),
        )
        return self._public_document(task_id, committed)

    def lock_for_start(
        self,
        task_id: str,
        *,
        reason: str = "launch_intent_accepted",
    ) -> dict[str, Any]:
        """Freeze the task baseline after a durable launch intent is published."""

        self.pin(task_id)
        return self.revisions.lock_current(
            task_id,
            locked_at=utc_now(),
            locked_reason=reason,
        )

    def resolve(self, task_id: str) -> ConfigSnapshot:
        current = self.revisions.read_current(task_id)
        if current is None:
            self.pin(task_id)
            current = self.revisions.read_current(task_id)
        assert current is not None
        return self._resolve_revision(current["revision"])

    def resolve_revision(self, task_id: str, revision_id: str) -> ConfigSnapshot:
        """Resolve one immutable task revision against process-only credentials."""

        revision = self.revisions.read_revision(task_id, revision_id)
        return self._resolve_revision(revision)

    def _resolve_revision(self, revision: dict[str, Any]) -> ConfigSnapshot:
        process = self.require_process_snapshot()
        raw_providers: dict[str, Any] = {}
        for name in revision["provider_ids"]:
            provider = process.providers.providers.get(name)
            if provider is None:
                raise HarnessError(
                    "MODEL_PROVIDER_NOT_AUTHORIZED",
                    "The task configuration references an unavailable Provider.",
                    {"provider": name},
                )
            raw_providers[name] = {
                "base_url": provider.base_url,
                "api_key": provider.api_key,
            }
        return ConfigSnapshot(
            schema_version="1.0",
            revision=revision["source_system_revision"],
            providers=ProviderConfig(schema_version="1.0", providers=raw_providers),
            model_list=ModelListConfig.model_validate(revision["model_list"]),
            runtime=RuntimeConfig.model_validate(revision["runtime"]),
        )

    def get_public(self, task_id: str) -> dict[str, Any]:
        current = self.revisions.read_current(task_id)
        if current is None:
            return self.pin(task_id)
        return self._public_document(task_id, current)

    def get_current(self, task_id: str) -> dict[str, Any]:
        current = self.revisions.read_current(task_id)
        if current is None:
            self.pin(task_id)
            current = self.revisions.read_current(task_id)
        assert current is not None
        return deepcopy(current)

    def source_citations_required(self, task_id: str) -> bool:
        document = self.get_public(task_id)
        return bool(document["runtime"]["document_processing"]["require_source_citations"])

    def require_process_snapshot(self) -> ConfigSnapshot:
        """Return validated deployment defaults without exposing them durably."""

        if self.process_snapshot is None:
            raise HarnessError(
                "VALIDATION_ERROR",
                "A validated deployment configuration is required for Master execution.",
            )
        return self.process_snapshot

    def _public_document(
        self, task_id: str, current: dict[str, Any]
    ) -> dict[str, Any]:
        if current.get("legacy"):
            legacy_path = (
                self.store.layout.control_root
                / "tasks"
                / task_id
                / "master"
                / "config-snapshot.json"
            )
            if legacy_path.exists():
                return load_legacy_task_snapshot(legacy_path)
        revision = current["revision"]
        return {
            "schema_version": "2.0",
            "task_id": task_id,
            "config_revision_id": revision["revision_id"],
            "source_config_revision": revision["source_system_revision"],
            "source_system_revision": revision["source_system_revision"],
            "config_hash": revision["config_hash"],
            "provider_ids": deepcopy(revision["provider_ids"]),
            "model_list": deepcopy(revision["model_list"]),
            "runtime": deepcopy(revision["runtime"]),
            "created_at": revision["created_at"],
            "locked_at": current["state"]["locked_at"],
            "locked_reason": current["state"]["locked_reason"],
            "revision": int(current["state"]["revision"]),
        }
