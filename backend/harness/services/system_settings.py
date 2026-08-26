"""Global, secret-free settings preview, publication and distribution."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..core.config import HarnessSettings
from ..core.config_kernel import (
    ConfigSnapshot,
    ConfigurationError,
    ImageAgentRuntimeFileConfig,
    RuntimeFileConfig,
    build_config_snapshot,
)
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import atomic_write_json, atomic_write_yaml, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.store import FileStateStore
from .agent_config_materialization import (
    RUNTIME_SETTING_FIELDS,
    ImageAgentConfigMaterializer,
)
from .instance_runtime_settings import InstanceRuntimeSettingsService
from .runtime_config_state import task_has_launch_evidence
from .task_config import TaskConfigService
from .task_config_rebase import TaskConfigRebaseService


class SystemSettingsService:
    """Publish validated global defaults without exposing Provider credentials."""

    def __init__(
        self,
        project_root: Path,
        settings: HarnessSettings,
        store: FileStateStore,
        task_config: TaskConfigService,
        materializer: ImageAgentConfigMaterializer,
        runtime_settings: InstanceRuntimeSettingsService,
        task_config_rebase: TaskConfigRebaseService,
    ) -> None:
        self.project_root = project_root
        self.config_root = project_root / "config"
        self.settings = settings
        self.store = store
        self.task_config = task_config
        self.materializer = materializer
        self.runtime_settings = runtime_settings
        self.task_config_rebase = task_config_rebase
        self.state_root = store.layout.control_root / "system-settings"
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get(self) -> dict[str, Any]:
        snapshot = self.task_config.require_process_snapshot()
        harness, image = self._documents(snapshot)
        publication = None
        state_path = self.state_root / "last-publication.json"
        if state_path.is_file():
            publication = read_json(state_path)
        return {
            "schema_version": "1.0",
            "revision": snapshot.revision,
            "harness_settings": harness,
            "image_agent_settings": image,
            "editable_schema": {
                "harness_settings": RuntimeFileConfig.model_json_schema(),
                "image_agent_settings": ImageAgentRuntimeFileConfig.model_json_schema(),
            },
            "model_options": self._model_options(snapshot),
            "last_publication": publication,
        }

    def preview(
        self,
        *,
        base_revision: str,
        harness_settings: dict[str, Any],
        image_agent_settings: dict[str, Any],
    ) -> dict[str, Any]:
        current = self.task_config.require_process_snapshot()
        if base_revision != current.revision:
            latest_harness, latest_image = self._documents(current)
            raise HarnessError(
                "SETTINGS_REVISION_CONFLICT",
                "The global settings changed after this page was loaded.",
                {
                    "latest_revision": current.revision,
                    "latest_harness_settings": latest_harness,
                    "latest_image_agent_settings": latest_image,
                },
            )
        candidate = self._candidate(current, harness_settings, image_agent_settings)
        current_harness, current_image = self._documents(current)
        candidate_harness, candidate_image = self._documents(candidate)
        changes = self._diff(
            {"harness_settings": current_harness, "image_agent_settings": current_image},
            {"harness_settings": candidate_harness, "image_agent_settings": candidate_image},
        )
        preview_id = digest_json(
            {
                "base_revision": base_revision,
                "candidate_revision": candidate.revision,
                "harness_settings": candidate_harness,
                "image_agent_settings": candidate_image,
            }
        )
        return {
            "schema_version": "1.0",
            "preview_id": f"settings-preview-{preview_id[:24]}",
            "base_revision": base_revision,
            "candidate_revision": candidate.revision,
            "changes": changes,
            "harness_settings": candidate_harness,
            "image_agent_settings": candidate_image,
        }

    def publish(
        self,
        *,
        preview_id: str,
        base_revision: str,
        harness_settings: dict[str, Any],
        image_agent_settings: dict[str, Any],
        actor: dict[str, str],
    ) -> dict[str, Any]:
        if actor.get("actor_type") != "human" or not actor.get("actor_id"):
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human operator may publish global settings."
            )
        with FileLock(
            self.store.layout.control_root / "locks" / "system-settings.lock",
            self.store.lock_timeout_seconds,
        ):
            preview = self.preview(
                base_revision=base_revision,
                harness_settings=harness_settings,
                image_agent_settings=image_agent_settings,
            )
            if preview["preview_id"] != preview_id:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The published settings no longer match the validated preview.",
                )
            current = self.task_config.require_process_snapshot()
            candidate = self._candidate(current, harness_settings, image_agent_settings)
            if candidate.revision == current.revision:
                return {
                    "schema_version": "1.0",
                    "status": "UNCHANGED",
                    "revision": current.revision,
                    "changes": [],
                    "distribution": {
                        "updated": 0,
                        "waiting_safe_point": 0,
                        "failed": 0,
                        "completed_history_unchanged": 0,
                        "items": [],
                    },
                }

            old_runtime = (self.config_root / "runtime.yaml").read_bytes()
            old_image = (self.config_root / "image_agent_runtime.yaml").read_bytes()
            try:
                atomic_write_yaml(
                    self.config_root / "runtime.yaml",
                    preview["harness_settings"],
                    mode=0o640,
                )
                atomic_write_yaml(
                    self.config_root / "image_agent_runtime.yaml",
                    preview["image_agent_settings"],
                    mode=0o640,
                )
            except BaseException:
                from ..storage.atomic import atomic_write_bytes

                atomic_write_bytes(self.config_root / "runtime.yaml", old_runtime, mode=0o640)
                atomic_write_bytes(
                    self.config_root / "image_agent_runtime.yaml", old_image, mode=0o640
                )
                raise

            self.task_config.process_snapshot = candidate
            self.settings.config_snapshot = candidate
            try:
                distribution = self._distribute(candidate, actor)
            except HarnessError as exc:
                distribution = self._failed_distribution(exc.code, exc.message)
            except (OSError, TypeError, ValueError):
                distribution = self._failed_distribution(
                    "CONFIG_DISTRIBUTION_FAILED",
                    "The global settings were published, but distribution did not complete.",
                )
            publication = {
                "schema_version": "1.0",
                "status": "PARTIAL" if distribution["failed"] else "PUBLISHED",
                "revision": candidate.revision,
                "previous_revision": current.revision,
                "published_at": utc_now(),
                "published_by": deepcopy(actor),
                "changes": preview["changes"],
                "distribution": distribution,
            }
            atomic_write_json(
                self.state_root / "last-publication.json", publication, mode=0o640
            )
            return publication

    def _candidate(
        self,
        current: ConfigSnapshot,
        harness_settings: dict[str, Any],
        image_agent_settings: dict[str, Any],
    ) -> ConfigSnapshot:
        try:
            return build_config_snapshot(
                providers=current.providers,
                model_list=current.model_list,
                runtime=harness_settings,
                image_agent_runtime=image_agent_settings,
            )
        except (ConfigurationError, ValidationError) as exc:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The global settings contain invalid values.",
                {"problems": str(exc).splitlines()[1:21]},
            ) from None

    def _distribute(
        self, candidate: ConfigSnapshot, actor: dict[str, str]
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        tasks_root = self.store.layout.control_root / "tasks"
        task_ids = sorted(
            path.name
            for path in tasks_root.iterdir()
            if path.is_dir() and not path.is_symlink() and (path / "task.json").is_file()
        ) if tasks_root.exists() else []
        for task_id in task_ids:
            plan = self.store.plan.get(task_id, task_id)
            if plan is None or not task_has_launch_evidence(self.store, task_id):
                continue
            for instance in plan["instances"]:
                if instance.get("agent_type") != "image":
                    continue
                instance_id = str(instance["instance_id"])
                if instance.get("status") in {
                    "SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED", "ARCHIVED", "CRASHED"
                }:
                    items.append(
                        {
                            "task_id": task_id,
                            "instance_id": instance_id,
                            "status": "COMPLETED_HISTORY_UNCHANGED",
                        }
                    )
                    continue
                try:
                    items.append(
                        self._distribute_instance(
                            task_id, instance_id, candidate, actor
                        )
                    )
                except HarnessError as exc:
                    items.append(
                        {
                            "task_id": task_id,
                            "instance_id": instance_id,
                            "status": "FAILED",
                            "error_code": exc.code,
                            "message": exc.message,
                        }
                    )
        rebase = self.task_config_rebase.rebase_all(
            actor={"actor_type": "system", "actor_id": "system_settings_publish"}
        )
        for item in rebase["items"]:
            if item["status"] in {"UPDATED", "CONFIG_REVIEW_REQUIRED"}:
                items.append(
                    {
                        "task_id": item["task_id"],
                        "instance_id": None,
                        "status": item["status"],
                    }
                )
        return {
            "updated": sum(
                item["status"]
                in {"APPLIED_BEFORE_START", "APPLIED_ON_BRANCH", "UPDATED"}
                for item in items
            ),
            "waiting_safe_point": sum(
                item["status"] == "WAITING_SAFE_POINT" for item in items
            ),
            "failed": sum(
                item["status"] in {"FAILED", "CONFIG_REVIEW_REQUIRED"}
                for item in items
            ),
            "completed_history_unchanged": sum(
                item["status"] == "COMPLETED_HISTORY_UNCHANGED" for item in items
            ),
            "items": items,
        }

    def _distribute_instance(
        self,
        task_id: str,
        instance_id: str,
        candidate: ConfigSnapshot,
        actor: dict[str, str],
    ) -> dict[str, Any]:
        current = self.materializer.revisions.read_current(task_id, instance_id)
        if current is None:
            self.materializer.materialize(task_id, instance_id)
            current = self.materializer.revisions.read_current(task_id, instance_id)
        assert current is not None
        task_revision = self.task_config.get_current(task_id)["revision"]
        managed_overrides = self._managed_overrides(candidate, task_revision)
        explicit_overrides = self._explicit_overrides(
            task_id,
            instance_id,
            current["manifest"]["overrides"],
        )
        target_overrides = self.materializer.merge_overrides(
            managed_overrides, explicit_overrides
        )
        patch = self._replacement_patch(
            current["manifest"]["overrides"], target_overrides
        )
        task_store_revision = self.store.task.revision(task_id, task_id)
        token = digest_json({"system_revision": candidate.revision})[:16]
        instance_token = digest_json(
            {"task_id": task_id, "instance_id": instance_id}
        )[:16]
        proposal = self.runtime_settings.propose(
            task_id,
            instance_id,
            base_revision=int(current["state"]["revision"]),
            patch=patch,
            sync_unstarted_image_work_items=False,
            expected_sync_instance_ids=[],
            envelope=CommandEnvelope(
                idempotency_key=f"global-preview-{token}-{instance_token}",
                actor_type="human",
                actor_id=str(actor["actor_id"]),
                expected_revision=task_store_revision,
            ),
        )
        applied = self.runtime_settings.confirm(
            task_id,
            instance_id,
            proposal["proposal_id"],
            envelope=CommandEnvelope(
                idempotency_key=f"global-publish-{token}-{instance_token}",
                actor_type="human",
                actor_id=str(actor["actor_id"]),
                expected_revision=task_store_revision,
            ),
        )
        atomic_write_json(
            self._instance_distribution_path(task_id, instance_id),
            {
                "schema_version": "1.0",
                "task_id": task_id,
                "instance_id": instance_id,
                "system_revision": candidate.revision,
                "managed_overrides": managed_overrides,
                "target_revision_id": applied["revision_id"],
                "status": applied["status"],
                "updated_at": utc_now(),
            },
            mode=0o640,
        )
        return {
            "task_id": task_id,
            "instance_id": instance_id,
            "status": applied["status"],
            "revision_id": applied["revision_id"],
            "branch_id": applied["branch_id"],
        }

    def _managed_overrides(
        self, candidate: ConfigSnapshot, task_revision: dict[str, Any]
    ) -> dict[str, Any]:
        candidate_task = {
            "runtime": candidate.runtime.model_dump(mode="json"),
            "model_list": candidate.model_list.model_dump(mode="json"),
        }
        baseline_runtime = self.materializer.effective_runtime(task_revision, {})
        desired_runtime = self.materializer.effective_runtime(candidate_task, {})
        overrides: dict[str, Any] = {}
        for field in RUNTIME_SETTING_FIELDS:
            if field == "advanced_model_overrides":
                continue
            if baseline_runtime[field] != desired_runtime[field]:
                overrides[field] = deepcopy(desired_runtime[field])
        baseline_models = self.runtime_settings.effective_model_ids(task_revision, {})
        desired_models = self.runtime_settings.effective_model_ids(candidate_task, {})
        model_overrides = {
            state: model_id
            for state, model_id in desired_models.items()
            if baseline_models[state] != model_id
        }
        if model_overrides:
            overrides["advanced_model_overrides"] = model_overrides
        return overrides

    def _explicit_overrides(
        self,
        task_id: str,
        instance_id: str,
        current_overrides: dict[str, Any],
    ) -> dict[str, Any]:
        path = self._instance_distribution_path(task_id, instance_id)
        if not path.is_file():
            return deepcopy(current_overrides)
        previous = read_json(path)
        managed = previous.get("managed_overrides") if isinstance(previous, dict) else None
        if not isinstance(managed, dict):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The global settings distribution record is malformed.",
                {"instance_id": instance_id},
            )
        explicit = deepcopy(current_overrides)
        for field, prior_value in managed.items():
            current_value = explicit.get(field)
            if isinstance(prior_value, dict) and isinstance(current_value, dict):
                for nested_field, nested_value in prior_value.items():
                    if current_value.get(nested_field) == nested_value:
                        current_value.pop(nested_field, None)
                if not current_value:
                    explicit.pop(field, None)
            elif current_value == prior_value:
                explicit.pop(field, None)
        return explicit

    @staticmethod
    def _replacement_patch(
        current: dict[str, Any], target: dict[str, Any]
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        for field in RUNTIME_SETTING_FIELDS:
            before = current.get(field)
            after = target.get(field)
            if before == after:
                continue
            if isinstance(before, dict) or isinstance(after, dict):
                before_nested = before if isinstance(before, dict) else {}
                after_nested = after if isinstance(after, dict) else {}
                patch[field] = {
                    key: deepcopy(after_nested[key]) if key in after_nested else None
                    for key in sorted(set(before_nested) | set(after_nested))
                }
            else:
                patch[field] = deepcopy(after) if field in target else None
        return patch

    def _instance_distribution_path(self, task_id: str, instance_id: str) -> Path:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        return self.state_root / "instances" / task_id / f"{instance_id}.json"

    @staticmethod
    def _failed_distribution(code: str, message: str) -> dict[str, Any]:
        return {
            "updated": 0,
            "waiting_safe_point": 0,
            "failed": 1,
            "completed_history_unchanged": 0,
            "items": [
                {
                    "task_id": None,
                    "instance_id": None,
                    "status": "FAILED",
                    "error_code": code,
                    "message": message,
                }
            ],
        }

    @staticmethod
    def _documents(snapshot: ConfigSnapshot) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime = snapshot.runtime.model_dump(mode="json")
        image = runtime.pop("image_agent")
        return runtime, {"schema_version": "1.0", **image}

    @staticmethod
    def _model_options(snapshot: ConfigSnapshot) -> dict[str, list[dict[str, str]]]:
        return {
            "text_models": [
                {"id": item.id, "label": item.label}
                for item in snapshot.model_list.text_models
            ],
            "vlm_models": [
                {"id": item.id, "label": item.label}
                for item in snapshot.model_list.vlm_models
            ],
            "image_models": [
                {"id": item.id, "label": item.label}
                for item in snapshot.model_list.image_models
            ],
        }

    @classmethod
    def _diff(
        cls, before: Any, after: Any, path: str = ""
    ) -> list[dict[str, Any]]:
        if isinstance(before, dict) and isinstance(after, dict):
            changes: list[dict[str, Any]] = []
            for key in sorted(set(before) | set(after)):
                next_path = f"{path}.{key}" if path else key
                changes.extend(cls._diff(before.get(key), after.get(key), next_path))
            return changes
        if before == after:
            return []
        return [{"field": path, "before": deepcopy(before), "after": deepcopy(after)}]
