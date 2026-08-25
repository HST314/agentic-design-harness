"""Rebase never-started task baselines after a system configuration publish."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .agent_config_materialization import ImageAgentConfigMaterializer
from .runtime_config_observability import RuntimeConfigObservability
from .runtime_config_state import (
    application_task_lock_path,
    is_unstarted_image_instance,
    task_has_launch_evidence,
)
from .task_config import TaskConfigService


class TaskConfigRebaseService:
    """Coordinate task and unstarted-instance revisions under the start lock."""

    def __init__(
        self,
        store: FileStateStore,
        task_config: TaskConfigService,
        materializer: ImageAgentConfigMaterializer,
        observability: RuntimeConfigObservability,
    ) -> None:
        self.store = store
        self.task_config = task_config
        self.materializer = materializer
        self.observability = observability

    def rebase_all(self, *, actor: dict[str, str]) -> dict[str, Any]:
        """Return bounded counts suitable for the system-settings control plane."""

        if actor.get("actor_type") not in {"human", "system"}:
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only a human or the system may publish task configuration defaults.",
            )
        tasks_root = self.store.layout.control_root / "tasks"
        task_ids = sorted(
            path.name
            for path in tasks_root.iterdir()
            if not path.is_symlink() and path.is_dir() and (path / "task.json").is_file()
        ) if tasks_root.exists() else []
        items: list[dict[str, Any]] = []
        for task_id in task_ids:
            items.append(self._rebase_one(task_id, actor=actor))
        summary = {
            "schema_version": "1.0",
            "source_system_revision": self.task_config.require_process_snapshot().revision,
            "updated": sum(item["status"] == "UPDATED" for item in items),
            "skipped_started": sum(item["status"] == "SKIPPED_STARTED" for item in items),
            "review_required": sum(item["status"] == "CONFIG_REVIEW_REQUIRED" for item in items),
            "unchanged": sum(item["status"] == "UNCHANGED" for item in items),
            "items": items,
        }
        return summary

    def _rebase_one(self, task_id: str, *, actor: dict[str, str]) -> dict[str, Any]:
        with FileLock(
            application_task_lock_path(self.store, task_id),
            self.store.lock_timeout_seconds,
        ):
            current = self.task_config.get_current(task_id)
            if current["state"]["locked_at"] is not None or task_has_launch_evidence(
                self.store, task_id
            ):
                self.observability.increment("task_rebase_skipped")
                return {"task_id": task_id, "status": "SKIPPED_STARTED", "conflicts": []}
            plan = self.store.plan.get(task_id, task_id)
            instances = [] if plan is None else plan["instances"]
            targets = [
                instance
                for instance in instances
                if is_unstarted_image_instance(self.store, task_id, instance)
            ]
            candidate = self._candidate_task_revision(task_id, current)
            conflicts = self._preflight_overrides(task_id, candidate, targets)
            if conflicts:
                self._write_review_required(task_id, current, conflicts, actor)
                self.observability.increment("task_rebase_conflicted")
                self.observability.event(
                    task_id,
                    "TASK_CONFIG_REBASE_REVIEW_REQUIRED",
                    actor=actor,
                    fields={
                        "old_hash": current["revision"]["config_hash"],
                        "new_hash": candidate["config_hash"],
                        "conflict_fields": sorted(
                            {field for item in conflicts for field in item["fields"]}
                        ),
                        "result": "CONFIG_REVIEW_REQUIRED",
                    },
                )
                return {
                    "task_id": task_id,
                    "status": "CONFIG_REVIEW_REQUIRED",
                    "conflicts": conflicts,
                }
            source_changed = (
                current["revision"]["source_system_revision"]
                != candidate["source_system_revision"]
            )
            old_hash = current["revision"]["config_hash"]
            task_public = self.task_config.rebase(
                task_id,
                created_by={"type": actor["actor_type"], "id": actor["actor_id"]},
            )
            instance_updates = self._rebase_instances(task_id, targets, actor)
            review_path = self._review_path(task_id)
            review_path.unlink(missing_ok=True)
            if not source_changed and not instance_updates:
                return {"task_id": task_id, "status": "UNCHANGED", "conflicts": []}
            self.observability.increment("task_rebase_succeeded")
            self.observability.event(
                task_id,
                "TASK_CONFIG_REBASED",
                actor=actor,
                fields={
                    "old_hash": old_hash,
                    "new_hash": task_public["config_hash"],
                    "task_config_revision_id": task_public["config_revision_id"],
                    "instance_count": len(instance_updates),
                    "result": "UPDATED",
                },
            )
            return {
                "task_id": task_id,
                "status": "UPDATED",
                "task_config_revision_id": task_public["config_revision_id"],
                "instance_ids": instance_updates,
                "conflicts": [],
            }

    def _candidate_task_revision(
        self, task_id: str, current: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = self.task_config.require_process_snapshot()
        current_revision = current["revision"]
        sequence = int(str(current_revision["revision_id"]).rsplit("r", 1)[1])
        if current_revision["source_system_revision"] != snapshot.revision:
            sequence += 1
        return self.task_config.revisions.build_revision(
            task_id=task_id,
            revision_id=f"task-config-r{sequence:06d}",
            parent_revision_id=(
                current_revision["parent_revision_id"]
                if current_revision["source_system_revision"] == snapshot.revision
                else current_revision["revision_id"]
            ),
            source_system_revision=snapshot.revision,
            provider_ids=sorted(snapshot.providers.providers),
            model_list=snapshot.model_list.model_dump(mode="json"),
            runtime=snapshot.runtime.model_dump(mode="json"),
            created_by={"type": "system", "id": "task_config_rebase_preview"},
            created_at=utc_now(),
        )

    def _preflight_overrides(
        self,
        task_id: str,
        candidate: dict[str, Any],
        targets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        conflicts: list[dict[str, Any]] = []
        for instance in targets:
            instance_id = instance["instance_id"]
            current = self.materializer.revisions.read_current(task_id, instance_id)
            overrides = {} if current is None else current["manifest"]["overrides"]
            try:
                self.materializer.validate_overrides(
                    candidate,
                    overrides,
                    require_current_approval=True,
                )
            except HarnessError as exc:
                if exc.code not in {"MODEL_NOT_APPROVED", "MODEL_PROVIDER_NOT_AUTHORIZED"}:
                    raise
                state = str(exc.details.get("state", "advanced_model_overrides"))
                conflicts.append(
                    {
                        "instance_id": instance_id,
                        "code": exc.code,
                        "fields": [f"advanced_model_overrides.{state}"],
                    }
                )
        return conflicts

    def _rebase_instances(
        self,
        task_id: str,
        targets: list[dict[str, Any]],
        actor: dict[str, str],
    ) -> list[str]:
        target_task_revision = self.task_config.get_current(task_id)["revision"]["revision_id"]
        updated: list[str] = []
        for instance in targets:
            instance_id = instance["instance_id"]
            current = self.materializer.revisions.read_current(task_id, instance_id)
            if current is None:
                self.materializer.materialize(task_id, instance_id)
                current = self.materializer.revisions.read_current(task_id, instance_id)
                assert current is not None
                updated.append(instance_id)
            if current["manifest"]["task_config_revision_id"] == target_task_revision:
                if (
                    self._reconcile_instance_projection(task_id, instance_id, current, actor)
                    and instance_id not in updated
                ):
                    updated.append(instance_id)
                continue
            if current["state"].get("pending_revision_id") is not None:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "An unstarted instance has a pending runtime configuration revision.",
                    {"instance_id": instance_id},
                )
            bundle = self.materializer.build_revision(
                task_id,
                instance_id,
                overrides=current["manifest"]["overrides"],
                created_by={"type": actor["actor_type"], "id": actor["actor_id"]},
                apply_mode="before_start",
                apply_status="APPLIED",
                confirmed_at=utc_now(),
                effective_from_state="initial",
                require_current_approval=True,
            )
            self.materializer.publish_revision(bundle)
            state = self.materializer.revisions.set_current(
                task_id,
                instance_id,
                bundle["manifest"]["revision_id"],
                expected_revision=int(current["state"]["revision"]),
                updated_at=bundle["manifest"]["created_at"],
            )
            self._reconcile_instance_projection(task_id, instance_id, bundle, actor)
            if state["current_revision_id"] == bundle["manifest"]["revision_id"]:
                updated.append(instance_id)
        return updated

    def _reconcile_instance_projection(
        self,
        task_id: str,
        instance_id: str,
        current: dict[str, Any],
        actor: dict[str, str],
    ) -> bool:
        sequence = int(current["manifest"]["revision_id"].rsplit("r", 1)[1])
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None:
            raise HarnessError(
                "INSTANCE_NOT_FOUND",
                "The rebased runtime configuration lost its instance projection.",
                {"instance_id": instance_id},
            )
        if int(instance["config_revision"]) == sequence:
            return False
        self.store.update_instance_fields(
            task_id,
            instance_id,
            {"config_revision": sequence},
            actor=Actor(actor["actor_type"], actor["actor_id"]),
            command="rebase_instance_runtime_config",
            idempotency_key=f"config-rebase-{current['manifest']['revision_id']}",
        )
        return True

    def _write_review_required(
        self,
        task_id: str,
        current: dict[str, Any],
        conflicts: list[dict[str, Any]],
        actor: dict[str, str],
    ) -> None:
        atomic_write_json(
            self._review_path(task_id),
            {
                "schema_version": "1.0",
                "task_id": task_id,
                "status": "CONFIG_REVIEW_REQUIRED",
                "current_revision_id": current["revision"]["revision_id"],
                "target_system_revision": self.task_config.require_process_snapshot().revision,
                "conflicts": deepcopy(conflicts),
                "actor": deepcopy(actor),
                "updated_at": utc_now(),
            },
            mode=0o640,
        )

    def _review_path(self, task_id: str):
        return (
            self.store.layout.control_root
            / "tasks"
            / task_id
            / "master"
            / "config"
            / "review-required.json"
        )
