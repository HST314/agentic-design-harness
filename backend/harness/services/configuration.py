"""Versioned global and per-instance runtime configuration projections."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, atomic_write_yaml, digest_json, read_json
from ..storage.locks import FileLock
from ..storage.ndjson import append_record, recover_records
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore

ApplyConfig = Callable[[str, str, Path], bool]
_SENSITIVE_CONFIG_KEY = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token|url|endpoint|host)",
    re.I,
)
_SENSITIVE_CONFIG_VALUE = re.compile(
    r"(?:bearer\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,})", re.I
)


class ReleasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release: Literal["auto", "manual", "off"] = "auto"


class SelfCheckPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    termination: Literal["fix", "solo"] = "solo"
    fixed_rounds: int = Field(default=2, ge=1, le=20)
    max_rounds: int = Field(default=4, ge=1, le=50)
    stop_early_on_pass: bool = False
    release: Literal["auto", "manual"] = "auto"

    @model_validator(mode="after")
    def validate_rounds(self) -> SelfCheckPolicy:
        if self.fixed_rounds > self.max_rounds:
            raise ValueError("fixed_rounds cannot exceed max_rounds")
        return self


class ImageRuntimePolicy(BaseModel):
    """Strict allow-list mapped to the Image Agent runtime policy file."""

    model_config = ConfigDict(extra="forbid")

    max_auto_questions: int = Field(default=3, ge=0, le=10)
    stream_model_output: Literal[False] = False
    clarification_total_budget: int = Field(default=10, ge=0, le=100)
    question_preference: Literal["proactive", "blocking_only"] = "proactive"
    category_constraint: ReleasePolicy = Field(default_factory=ReleasePolicy)
    style_direction: ReleasePolicy = Field(default_factory=ReleasePolicy)
    skill_invocation: ReleasePolicy = Field(default_factory=ReleasePolicy)
    self_check: SelfCheckPolicy = Field(default_factory=SelfCheckPolicy)
    max_render_retries: Literal[0] = 0
    candidate_concurrency: int = Field(default=5, ge=1, le=5)
    model_timeout_seconds: float = Field(default=180, gt=0, le=3600)
    default_output_size: str = Field(default="2560x1440", pattern=r"^(\d{2,5}x\d{2,5}|[124]K)$")
    response_format: Literal["url", "b64_json"] = "url"
    watermark: bool = False
    offline_mode: bool = True
    allow_skill_degradation: bool = False
    style_library_root: str = Field(default="agent-library", min_length=1, max_length=512)

    @field_validator("style_library_root")
    @classmethod
    def relative_library_root(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError("style_library_root must be a safe relative path")
        return path.as_posix()


class ModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,127}$")
    model_role: Literal["reasoning_llm", "text_to_image_model", "vision_language_model"]
    provider: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    model: str = Field(min_length=1, max_length=256)
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    fallback_model: str | None = Field(default=None, max_length=256)

    @field_validator("parameters")
    @classmethod
    def safe_parameters(
        cls, value: dict[str, str | int | float | bool]
    ) -> dict[str, str | int | float | bool]:
        if any(_SENSITIVE_CONFIG_KEY.search(key) for key in value):
            raise ValueError("credential-like model parameters are not controlled config fields")
        if any(
            isinstance(item, str) and _SENSITIVE_CONFIG_VALUE.search(item)
            for item in value.values()
        ):
            raise ValueError("credential-like values are not controlled config fields")
        return value


class ImageModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_config_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    state_bindings: list[ModelBinding] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_states(self) -> ImageModelConfig:
        states = [item.state for item in self.state_bindings]
        if len(states) != len(set(states)):
            raise ValueError("Image model binding states must be unique")
        return self


class SupervisorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    port_range_start: int = Field(default=18100, ge=1024, le=65535)
    port_range_end: int = Field(default=18199, ge=1024, le=65535)
    startup_timeout_seconds: float = Field(default=15, gt=0, le=300)
    health_interval_seconds: float = Field(default=1, gt=0, le=60)
    shutdown_grace_seconds: float = Field(default=5, gt=0, le=60)

    @model_validator(mode="after")
    def validate_range(self) -> SupervisorConfig:
        if self.port_range_start > self.port_range_end:
            raise ValueError("port range start must not exceed end")
        return self


class GlobalConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    image_provider: str = Field(default="fake", pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    image_runtime_policy: ImageRuntimePolicy = Field(default_factory=ImageRuntimePolicy)
    image_model_config: ImageModelConfig = Field(
        default_factory=lambda: ImageModelConfig(model_config_id="offline_fake")
    )
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)

    @model_validator(mode="after")
    def one_provider_per_instance(self) -> GlobalConfigBody:
        providers = {item.provider for item in self.image_model_config.state_bindings}
        if providers and providers != {self.image_provider}:
            raise ValueError("all Image bindings must match the configured credential Provider")
        return self


class HarnessGlobalConfig(GlobalConfigBody):
    revision: int = Field(ge=1)


class ConfigurationService:
    """Uses config events as commits and YAML/instance files as projections."""

    def __init__(self, store: FileStateStore, apply_config: ApplyConfig | None = None) -> None:
        self.store = store
        self.apply_config = apply_config
        self.global_path = store.layout.control_root / "config" / "global.yaml"
        self.events_path = store.layout.control_root / "config-events.ndjson"
        self.lock_path = store.layout.control_root / "locks" / "global-config.lock"

    def initialize(self, body: dict[str, Any] | GlobalConfigBody | None = None) -> dict[str, Any]:
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            existing = self.get_global(required=False)
            if existing is not None:
                return existing
            validated = self._body(body or {})
            snapshot = HarnessGlobalConfig(
                **validated.model_dump(mode="json"), revision=1
            ).model_dump(mode="json")
            event = {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "GLOBAL_CONFIG_COMMITTED",
                "revision": 1,
                "config": snapshot,
                "targets": [],
                "actor": Actor("system", "configuration_init").as_dict(),
                "idempotency_key": "initialize-global-config",
                "committed_at": utc_now(),
            }
            append_record(self.events_path, event)
            self._materialize_global(event)
            return deepcopy(snapshot)

    def get_global(self, *, required: bool = True) -> dict[str, Any] | None:
        if not self.global_path.exists():
            if required:
                raise HarnessError("VALIDATION_ERROR", "Global configuration is not initialized.")
            return None
        try:
            loaded = yaml.safe_load(self.global_path.read_text(encoding="utf-8"))
            return HarnessGlobalConfig.model_validate(loaded).model_dump(mode="json")
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError):
            raise HarnessError(
                "VALIDATION_ERROR", "The persisted global configuration is invalid."
            ) from None

    def create_instance_snapshot(self, task_id: str, instance_id: str) -> dict[str, Any]:
        with FileLock(self.lock_path, self.store.lock_timeout_seconds), FileLock(
            self._task_config_lock(task_id), self.store.lock_timeout_seconds
        ):
            instance = self._instance(task_id, instance_id)
            existing = self._read_instance_config(task_id, instance_id)
            if existing is not None:
                return existing
            global_config = self.get_global()
            assert global_config is not None
            snapshot = self._instance_snapshot(
                task_id,
                instance_id,
                config_revision=1,
                source_global_revision=global_config["revision"],
                scope="global",
                body=self._body(global_config),
                restart_required=False,
            )
            event = {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "INSTANCE_CONFIG_COMMITTED",
                "task_id": task_id,
                "instance_id": instance_id,
                "snapshot": snapshot,
                "actor": Actor("system", "instance_creation").as_dict(),
                "idempotency_key": f"initial-config-{instance_id}",
                "committed_at": utc_now(),
            }
            append_record(self._task_events(task_id), event)
            self._materialize_instance_config(snapshot, instance)
        return deepcopy(snapshot)

    def update_instance(
        self,
        task_id: str,
        instance_id: str,
        patch: dict[str, Any],
        *,
        expected_revision: int,
        idempotency_key: str,
        actor: Actor,
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 128:
            raise HarnessError("VALIDATION_ERROR", "Invalid config idempotency key.")
        if "revision" in patch:
            raise HarnessError("VALIDATION_ERROR", "Config revisions are service-controlled.")
        if self._instance(task_id, instance_id)["status"] == "ARCHIVED":
            existing_event = self._instance_config_event(task_id, idempotency_key)
            if existing_event is not None:
                expected_digest = self._config_request_digest(
                    instance_id, patch, expected_revision
                )
                if existing_event["request_sha256"] != expected_digest:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The config idempotency key was reused for a different request.",
                    )
                return self._effective_instance_snapshot(existing_event["snapshot"])
            raise HarnessError(
                "INVALID_STATE_TRANSITION", "An archived instance is read-only."
            )
        if self._read_instance_config(task_id, instance_id) is None:
            self.create_instance_snapshot(task_id, instance_id)
        with FileLock(self._task_config_lock(task_id), self.store.lock_timeout_seconds):
            existing_event = self._instance_config_event(task_id, idempotency_key)
            if existing_event is not None:
                expected_digest = self._config_request_digest(instance_id, patch, expected_revision)
                if existing_event["request_sha256"] != expected_digest:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The config idempotency key was reused for a different request.",
                    )
                self._materialize_instance_config(
                    existing_event["snapshot"], self._instance(task_id, instance_id)
                )
                return self._effective_instance_snapshot(existing_event["snapshot"])
            instance = self._instance(task_id, instance_id)
            if instance["status"] == "ARCHIVED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION", "An archived instance is read-only."
                )
            current = self._read_instance_config(task_id, instance_id)
            if current is None:
                raise RuntimeError("instance config initialization did not commit")
            if current["config_revision"] != expected_revision:
                self._revision_error(expected_revision, current["config_revision"])
            merged = deep_merge(current["config"], patch)
            body = self._body(merged)
            restart_required = self._requires_restart(instance)
            snapshot = self._instance_snapshot(
                task_id,
                instance_id,
                config_revision=expected_revision + 1,
                source_global_revision=current["source_global_revision"],
                scope="instance",
                body=body,
                restart_required=restart_required,
            )
            event = {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": "INSTANCE_CONFIG_COMMITTED",
                "task_id": task_id,
                "instance_id": instance_id,
                "snapshot": snapshot,
                "request_sha256": self._config_request_digest(
                    instance_id, patch, expected_revision
                ),
                "actor": actor.as_dict(),
                "idempotency_key": idempotency_key,
                "committed_at": utc_now(),
            }
            append_record(self._task_events(task_id), event)
            return self._materialize_instance_config(snapshot, instance)

    def save_global(
        self,
        body: dict[str, Any] | GlobalConfigBody,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor: Actor,
        crash_hook=None,
    ) -> dict[str, Any]:
        if not idempotency_key or len(idempotency_key) > 128:
            raise HarnessError("VALIDATION_ERROR", "Invalid config idempotency key.")
        validated = self._body(body)
        request_sha256 = self._global_request_digest(validated, expected_revision)
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            events = recover_records(self.events_path)
            existing = next(
                (
                    item
                    for item in events
                    if item.get("event_type") == "GLOBAL_CONFIG_COMMITTED"
                    and item.get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if existing is not None:
                if existing.get("request_sha256") != request_sha256:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The global-config idempotency key was reused.",
                    )
                current = self.get_global()
                if current is None or current["revision"] <= existing["revision"]:
                    with self._target_locks(existing["targets"]):
                        self._materialize_global(existing)
                return deepcopy(existing["config"])
            instances = self._all_instances()
            lock_targets = [
                {"task_id": item["task_id"]}
                for item in instances
                if item["status"] != "ARCHIVED"
            ]
            with self._target_locks(lock_targets):
                current = self.get_global()
                assert current is not None
                if current["revision"] != expected_revision:
                    self._revision_error(expected_revision, current["revision"])
                global_snapshot = HarnessGlobalConfig(
                    **validated.model_dump(mode="json"), revision=expected_revision + 1
                ).model_dump(mode="json")
                targets = []
                for instance in self._all_instances():
                    if instance["status"] == "ARCHIVED":
                        continue
                    current_instance = self._read_instance_config(
                        instance["task_id"], instance["instance_id"]
                    )
                    next_revision = (
                        current_instance["config_revision"] + 1
                        if current_instance
                        else 1
                    )
                    targets.append(
                        self._instance_snapshot(
                            instance["task_id"],
                            instance["instance_id"],
                            config_revision=next_revision,
                            source_global_revision=global_snapshot["revision"],
                            scope="global",
                            body=validated,
                            restart_required=self._requires_restart(instance),
                        )
                    )
                event = {
                    "event_id": f"evt_{uuid.uuid4().hex}",
                    "event_type": "GLOBAL_CONFIG_COMMITTED",
                    "revision": global_snapshot["revision"],
                    "config": global_snapshot,
                    "targets": targets,
                    "request_sha256": request_sha256,
                    "actor": actor.as_dict(),
                    "idempotency_key": idempotency_key,
                    "committed_at": utc_now(),
                }
                append_record(self.events_path, event)
                if crash_hook:
                    crash_hook("after_global_config_event")
                self._materialize_global(event, crash_hook=crash_hook)
                return deepcopy(global_snapshot)

    def recover(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            global_events = [
                item
                for item in recover_records(self.events_path)
                if item.get("event_type") == "GLOBAL_CONFIG_COMMITTED"
            ]
            if global_events:
                latest = max(global_events, key=lambda item: item["revision"])
                self._materialize_global(latest)
                recovered.append({"global_revision": latest["revision"]})
            tasks_root = self.store.layout.control_root / "tasks"
            for task_dir in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
                latest_by_instance: dict[str, dict[str, Any]] = {}
                applied_by_instance: dict[str, dict[str, Any]] = {}
                for event in recover_records(task_dir / "events.ndjson"):
                    if event.get("event_type") == "INSTANCE_CONFIG_COMMITTED":
                        snapshot = event["snapshot"]
                        current = latest_by_instance.get(snapshot["instance_id"])
                        if (
                            current is None
                            or snapshot["config_revision"] > current["config_revision"]
                        ):
                            latest_by_instance[snapshot["instance_id"]] = snapshot
                    elif event.get("event_type") == "INSTANCE_CONFIG_APPLIED":
                        applied_by_instance[event["instance_id"]] = event
                for raw_snapshot in latest_by_instance.values():
                    snapshot = deepcopy(raw_snapshot)
                    applied = applied_by_instance.get(snapshot["instance_id"])
                    if (
                        applied is not None
                        and applied["config_revision"] == snapshot["config_revision"]
                    ):
                        snapshot.update(
                            {
                                "restart_required": False,
                                "applied_at": applied["applied_at"],
                            }
                        )
                    instance = self.store.instance.get(
                        snapshot["task_id"], snapshot["instance_id"]
                    )
                    if instance is not None and instance["status"] != "ARCHIVED":
                        self._materialize_instance_config(snapshot, instance)
                        recovered.append(
                            {
                                "task_id": snapshot["task_id"],
                                "instance_id": snapshot["instance_id"],
                                "config_revision": snapshot["config_revision"],
                            }
                        )
        return recovered

    def mark_restarted(
        self, task_id: str, instance_id: str, *, applied_revision: int | None = None
    ) -> dict[str, Any]:
        with FileLock(self._task_config_lock(task_id), self.store.lock_timeout_seconds):
            snapshot = self._read_instance_config(task_id, instance_id)
            if snapshot is None:
                raise HarnessError("VALIDATION_ERROR", "Instance configuration is missing.")
            if applied_revision is not None and snapshot["config_revision"] != applied_revision:
                return snapshot
            if not snapshot["restart_required"]:
                return snapshot
            event = self._record_config_applied(snapshot, "process_supervisor")
            updated = {
                **snapshot,
                "restart_required": False,
                "applied_at": event["applied_at"],
            }
            instance_root = self.store.layout.initialize_instance(task_id, instance_id)
            atomic_write_json(
                instance_root / "runtime-config.json", updated, mode=0o640
            )
            atomic_write_json(self._instance_config_path(task_id, instance_id), updated)
            self.store.update_instance_fields(
                task_id,
                instance_id,
                {"restart_required": False},
                actor=Actor("system", "process_supervisor"),
                command="clear_config_restart_required",
                idempotency_key=(
                    f"config-restarted-{instance_id}-{snapshot['config_revision']}"
                ),
            )
            return updated

    def image_runtime_files(self, snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
        body = self._body(snapshot.get("config", snapshot))
        runtime = body.image_runtime_policy.model_dump(mode="json")
        runtime["image_api_base_url"] = ""
        return {
            "runtime.yaml": runtime,
            "model-config.yaml": body.image_model_config.model_dump(mode="json"),
        }

    def _materialize_global(self, event: dict[str, Any], crash_hook=None) -> None:
        atomic_write_yaml(self.global_path, event["config"], mode=0o600)
        for index, snapshot in enumerate(event["targets"]):
            instance = self.store.instance.get(snapshot["task_id"], snapshot["instance_id"])
            if instance is None or instance["status"] == "ARCHIVED":
                continue
            self._materialize_instance_config(snapshot, instance)
            if crash_hook:
                crash_hook(f"after_global_target_{index + 1}")

    def _materialize_instance_config(
        self, snapshot: dict[str, Any], instance: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = self._effective_instance_snapshot(snapshot)
        current = self._read_instance_config(snapshot["task_id"], snapshot["instance_id"])
        if current is not None and current["config_revision"] > snapshot["config_revision"]:
            return current

        instance_root = self.store.layout.initialize_instance(
            snapshot["task_id"], snapshot["instance_id"]
        )
        runtime_root = instance_root / "runtime"
        for filename, payload in self.image_runtime_files(snapshot).items():
            atomic_write_yaml(runtime_root / filename, payload, mode=0o640)
        atomic_write_json(instance_root / "runtime-config.json", snapshot, mode=0o640)
        if snapshot["restart_required"] and self.apply_config is not None:
            try:
                hot_applied = self.apply_config(
                    snapshot["task_id"],
                    snapshot["instance_id"],
                    instance_root / "runtime-config.json",
                )
            except Exception:
                hot_applied = False
            if hot_applied:
                snapshot["restart_required"] = False
                event = self._record_config_applied(snapshot, "config_adapter")
                snapshot["applied_at"] = event["applied_at"]
                atomic_write_json(instance_root / "runtime-config.json", snapshot, mode=0o640)
        atomic_write_json(
            self._instance_config_path(snapshot["task_id"], snapshot["instance_id"]),
            snapshot,
        )
        self.store.update_instance_fields(
            snapshot["task_id"],
            snapshot["instance_id"],
            {
                "config_revision": snapshot["config_revision"],
                "restart_required": snapshot["restart_required"],
            },
            actor=Actor("system", "configuration_projection"),
            command="materialize_instance_config",
            idempotency_key=(
                f"config-{snapshot['instance_id']}-{snapshot['config_revision']}"
                + ("-applied" if snapshot["applied_at"] is not None else "")
            ),
        )
        return deepcopy(snapshot)

    def _effective_instance_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        snapshot = deepcopy(snapshot)
        applied = self._config_applied_event(
            snapshot["task_id"],
            snapshot["instance_id"],
            snapshot["config_revision"],
        )
        if applied is not None:
            snapshot.update(
                {"restart_required": False, "applied_at": applied["applied_at"]}
            )
        return snapshot

    @staticmethod
    def _instance_snapshot(
        task_id: str,
        instance_id: str,
        *,
        config_revision: int,
        source_global_revision: int,
        scope: Literal["global", "instance"],
        body: GlobalConfigBody,
        restart_required: bool,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            "instance_id": instance_id,
            "config_revision": config_revision,
            "source_global_revision": source_global_revision,
            "scope": scope,
            "config": body.model_dump(mode="json"),
            "restart_required": restart_required,
            "committed_at": utc_now(),
            "applied_at": None,
        }

    def _all_instances(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        root = self.store.layout.control_root / "tasks"
        for task_dir in sorted(root.iterdir() if root.exists() else []):
            if task_dir.is_dir():
                values.extend(self.store.instance.list(task_dir.name))
        return values

    def _instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        return instance

    def _read_instance_config(
        self, task_id: str, instance_id: str
    ) -> dict[str, Any] | None:
        path = self._instance_config_path(task_id, instance_id)
        return read_json(path) if path.exists() else None

    def _instance_config_path(self, task_id: str, instance_id: str) -> Path:
        path = self.store.layout.control_root / "tasks" / task_id / "instance-configs"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path / f"{instance_id}.json"

    def _task_events(self, task_id: str) -> Path:
        return self.store.layout.control_root / "tasks" / task_id / "events.ndjson"

    def _task_config_lock(self, task_id: str) -> Path:
        return self.store.layout.control_root / "locks" / f"config-{task_id}.lock"

    @contextmanager
    def _target_locks(self, targets: list[dict[str, Any]]):
        with ExitStack() as stack:
            for task_id in sorted({item["task_id"] for item in targets}):
                stack.enter_context(
                    FileLock(
                        self._task_config_lock(task_id),
                        self.store.lock_timeout_seconds,
                    )
                )
            yield

    def _instance_config_event(
        self, task_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in recover_records(self._task_events(task_id))
                if item.get("event_type") == "INSTANCE_CONFIG_COMMITTED"
                and item.get("idempotency_key") == idempotency_key
            ),
            None,
        )

    def _record_config_applied(
        self, snapshot: dict[str, Any], actor_id: str
    ) -> dict[str, Any]:
        existing = self._config_applied_event(
            snapshot["task_id"],
            snapshot["instance_id"],
            snapshot["config_revision"],
        )
        if existing is not None:
            return existing
        applied_at = utc_now()
        event = {
            "event_id": f"evt_{uuid.uuid4().hex}",
            "event_type": "INSTANCE_CONFIG_APPLIED",
            "task_id": snapshot["task_id"],
            "instance_id": snapshot["instance_id"],
            "config_revision": snapshot["config_revision"],
            "actor": Actor("system", actor_id).as_dict(),
            "applied_at": applied_at,
            "occurred_at": applied_at,
        }
        append_record(self._task_events(snapshot["task_id"]), event)
        return event

    def _config_applied_event(
        self, task_id: str, instance_id: str, config_revision: int
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in recover_records(self._task_events(task_id))
                if item.get("event_type") == "INSTANCE_CONFIG_APPLIED"
                and item.get("instance_id") == instance_id
                and item.get("config_revision") == config_revision
            ),
            None,
        )

    @staticmethod
    def _body(value: dict[str, Any] | GlobalConfigBody) -> GlobalConfigBody:
        if isinstance(value, GlobalConfigBody):
            return value
        cleaned = {key: item for key, item in value.items() if key != "revision"}
        try:
            return GlobalConfigBody.model_validate(cleaned)
        except ValidationError as exc:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The controlled global configuration is invalid.",
                {"error_count": exc.error_count()},
            ) from None

    @staticmethod
    def _global_request_digest(body: GlobalConfigBody, expected_revision: int) -> str:
        return digest_json(
            {"config": body.model_dump(mode="json"), "expected_revision": expected_revision}
        )

    @staticmethod
    def _config_request_digest(
        instance_id: str, patch: dict[str, Any], expected_revision: int
    ) -> str:
        return digest_json(
            {
                "instance_id": instance_id,
                "patch": patch,
                "expected_revision": expected_revision,
            }
        )

    @staticmethod
    def _requires_restart(instance: dict[str, Any]) -> bool:
        return instance["status"] in {"STARTING", "RUNNING", "WAITING_APPROVAL"}

    @staticmethod
    def _revision_error(expected: int, actual: int) -> None:
        raise HarnessError(
            "REVISION_CONFLICT",
            "The configuration revision changed before this command committed.",
            {"expected_revision": expected, "actual_revision": actual},
        )


def deep_merge(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(current)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged
