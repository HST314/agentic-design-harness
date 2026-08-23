"""One-way Image Agent configuration derived from a task snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from ..core.config_kernel import ConfigSnapshot, SupervisorConfig
from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_yaml, digest_json, set_permissions
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.safe_open import is_link_or_reparse
from ..storage.store import FileStateStore
from .task_config import TaskConfigService

_MATERIALIZATION_FIELDS = frozenset(
    {"source_config_revision", "config_hash", "generated_at"}
)
_STATE_ROUTES = (
    ("intake_clarify", "reasoning_llm", "text_models", "text_reasoning"),
    ("confirmation_build", "reasoning_llm", "text_models", "text_reasoning"),
    (
        "initial_candidate_generation",
        "text_to_image_model",
        "image_models",
        "image_generation",
    ),
    (
        "self_check_inspection",
        "vision_language_model",
        "vlm_models",
        "vision_understanding",
    ),
    ("self_check_rework", "text_to_image_model", "image_models", "image_generation"),
    ("human_prompt_rework", "text_to_image_model", "image_models", "image_generation"),
)
_PROVIDER_ENVIRONMENT = {"ark": ("ARK_BASE_URL", "ARK_API_KEY")}


@dataclass(frozen=True, slots=True)
class ImageAgentLaunchConfiguration:
    """Validated launch inputs; secret values never enter materialized files."""

    source_config_revision: str
    config_hash: str
    runtime_path: Path
    model_config_path: Path
    supervisor: SupervisorConfig
    provider_environment: Mapping[str, str] = field(repr=False)

    @property
    def redaction_values(self) -> tuple[str, ...]:
        return tuple(self.provider_environment.values())


class ImageAgentConfigMaterializer:
    """Expand root model choices into immutable, instance-local Agent files."""

    def __init__(self, store: FileStateStore, task_config: TaskConfigService) -> None:
        self.store = store
        self.task_config = task_config

    def materialize(self, task_id: str, instance_id: str) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        public_snapshot = self.task_config.get_public(task_id)
        runtime_body, model_body = self._materialized_bodies(public_snapshot)
        config_hash = digest_json({"runtime": runtime_body, "model_config": model_body})
        root = self._runtime_root(task_id, instance_id)
        lock_path = (
            self.store.layout.control_root
            / "locks"
            / f"image-config-{task_id}-{instance_id}.lock"
        )
        with FileLock(lock_path, self.store.lock_timeout_seconds):
            self._prepare_directory(root)
            runtime_path = root / "runtime.yaml"
            model_path = root / "model_config.yaml"
            generated_at = self._existing_generation(
                runtime_path,
                model_path,
                public_snapshot["source_config_revision"],
                config_hash,
                runtime_body,
                model_body,
            )
            write_required = generated_at is None
            metadata = {
                "source_config_revision": public_snapshot["source_config_revision"],
                "config_hash": config_hash,
                "generated_at": generated_at or utc_now(),
            }
            try:
                if write_required:
                    atomic_write_yaml(runtime_path, {**metadata, **runtime_body}, mode=0o400)
                    atomic_write_yaml(model_path, {**metadata, **model_body}, mode=0o400)
            finally:
                self._make_read_only(root)
        return {
            **metadata,
            "runtime_path": runtime_path,
            "model_config_path": model_path,
        }

    def resolve_launch(self, task_id: str, instance_id: str) -> ImageAgentLaunchConfiguration:
        materialized = self.materialize(task_id, instance_id)
        snapshot = self.task_config.resolve(task_id)
        if snapshot.revision != materialized["source_config_revision"]:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Image Agent materialization does not match its task configuration.",
                {"instance_id": instance_id},
            )
        provider_environment = self._provider_environment(snapshot)
        return ImageAgentLaunchConfiguration(
            source_config_revision=snapshot.revision,
            config_hash=materialized["config_hash"],
            runtime_path=materialized["runtime_path"],
            model_config_path=materialized["model_config_path"],
            supervisor=snapshot.runtime.supervisor,
            provider_environment=provider_environment,
        )

    def _runtime_root(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.initialize_instance(task_id, instance_id)
            / "runtime-config"
        )

    @staticmethod
    def _prepare_directory(root: Path) -> None:
        if root.exists() and (not root.is_dir() or is_link_or_reparse(root)):
            raise HarnessError(
                "PATH_OUTSIDE_TASK_ROOT",
                "The Image Agent runtime configuration directory is unsafe.",
            )
        root.mkdir(parents=False, exist_ok=True, mode=0o700)
        set_permissions(root, 0o700)
        for path in (root / "runtime.yaml", root / "model_config.yaml"):
            if path.exists() and (not path.is_file() or is_link_or_reparse(path)):
                raise HarnessError(
                    "PATH_OUTSIDE_TASK_ROOT",
                    "Image Agent runtime configuration files must be regular files.",
                )

    @staticmethod
    def _make_read_only(root: Path) -> None:
        for path in (root / "runtime.yaml", root / "model_config.yaml"):
            if path.exists():
                set_permissions(path, 0o400)
        set_permissions(root, 0o500)

    @classmethod
    def _existing_generation(
        cls,
        runtime_path: Path,
        model_path: Path,
        revision: str,
        config_hash: str,
        runtime_body: dict[str, Any],
        model_body: dict[str, Any],
    ) -> str | None:
        runtime = cls._read_yaml(runtime_path)
        model = cls._read_yaml(model_path)
        if runtime is None or model is None:
            return None
        generated_at = runtime.get("generated_at")
        if not isinstance(generated_at, str) or model.get("generated_at") != generated_at:
            return None
        expected_metadata = {
            "source_config_revision": revision,
            "config_hash": config_hash,
        }
        for document, body in ((runtime, runtime_body), (model, model_body)):
            if any(document.get(key) != value for key, value in expected_metadata.items()):
                return None
            document_body = {
                key: value
                for key, value in document.items()
                if key not in _MATERIALIZATION_FIELDS
            }
            if document_body != body:
                return None
        return generated_at

    @staticmethod
    def _read_yaml(path: Path) -> dict[str, Any] | None:
        if not path.is_file() or is_link_or_reparse(path):
            return None
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        return value if isinstance(value, dict) else None

    @classmethod
    def _materialized_bodies(
        cls, task_snapshot: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        runtime = task_snapshot["runtime"]
        image_agent = runtime["image_agent"]
        runtime_body = {
            "question_preference": {
                "proactive": "proactive",
                "on_demand": "blocking_only",
            }[image_agent["question_preference"]],
            "candidate_concurrency": image_agent["candidate_concurrency"],
            "default_output_size": image_agent["default_output_size"],
            "response_format": image_agent["response_format"],
            "watermark": image_agent["watermark"],
            "offline_mode": False,
            # Harness owns the external approval boundary. The child must stop
            # after a passing inspection instead of attempting final delivery.
            "self_check": {"release": "manual"},
        }
        groups = {
            name: {item["id"]: item for item in task_snapshot["model_list"][name]}
            for name in ("text_models", "vlm_models", "image_models")
        }
        overrides = image_agent["advanced_model_overrides"]
        bindings = []
        for state, role, group, default_name in _STATE_ROUTES:
            selected_id = overrides[state] or runtime["models"][default_name]
            model = groups[group][selected_id]
            bindings.append(cls._binding(state, role, model))
        model_body = {
            "model_config_id": "materialized-" + task_snapshot["source_config_revision"],
            "state_bindings": bindings,
        }
        return runtime_body, model_body

    @staticmethod
    def _binding(state: str, role: str, model: dict[str, Any]) -> dict[str, Any]:
        return {
            "state": state,
            "model_role": role,
            "provider": model["provider"],
            "model": model["model"],
            "parameters": dict(model["parameters"]),
            "fallback_model": None,
        }

    @staticmethod
    def _provider_environment(snapshot: ConfigSnapshot) -> Mapping[str, str]:
        provider_names = {
            model.provider
            for group in (
                snapshot.model_list.text_models,
                snapshot.model_list.vlm_models,
                snapshot.model_list.image_models,
            )
            for model in group
        }
        environment: dict[str, str] = {}
        for name in sorted(provider_names):
            try:
                base_url_env, api_key_env = _PROVIDER_ENVIRONMENT[name]
                provider = snapshot.providers.providers[name]
            except KeyError:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "The task configuration references an unsupported Image Agent Provider.",
                    {"provider": name},
                ) from None
            environment[base_url_env] = provider.base_url
            environment[api_key_env] = provider.api_key.get_secret_value()
        return MappingProxyType(environment)
