"""Immutable Image Agent runtime revision materialization."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ..core.config_kernel import ConfigSnapshot, SupervisorConfig
from ..core.errors import HarnessError
from ..storage.instance_config_revisions import InstanceConfigRevisionStore
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.store import FileStateStore
from .task_config import TaskConfigService

_STATE_ROUTES = (
    ("intake_clarify", "reasoning_llm", "text_models", "text_reasoning", "structured_output"),
    ("confirmation_build", "reasoning_llm", "text_models", "text_reasoning", "structured_output"),
    (
        "initial_candidate_generation",
        "text_to_image_model",
        "image_models",
        "image_generation",
        "text_to_image",
    ),
    (
        "self_check_inspection",
        "vision_language_model",
        "vlm_models",
        "vision_understanding",
        "image_input",
    ),
    (
        "self_check_rework",
        "text_to_image_model",
        "image_models",
        "image_generation",
        "text_to_image",
    ),
    (
        "human_prompt_rework",
        "text_to_image_model",
        "image_models",
        "image_generation",
        "text_to_image",
    ),
)
_MODEL_STATES = tuple(item[0] for item in _STATE_ROUTES)
_PROVIDER_ENVIRONMENT = {"ark": ("ARK_BASE_URL", "ARK_API_KEY")}
_DEFAULT_SELF_CHECK = {
    "termination": "solo",
    "fixed_rounds": 2,
    "max_rounds": 4,
    "stop_early_on_pass": False,
}
_OUTPUT_SIZE = re.compile(r"^(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)$")


@dataclass(frozen=True, slots=True)
class ImageAgentLaunchConfiguration:
    """Validated launch inputs; secret values never enter materialized files."""

    source_config_revision: str
    task_config_revision_id: str
    runtime_config_revision_id: str
    config_hash: str
    runtime_path: Path
    model_config_path: Path
    config_root: Path | None
    supervisor: SupervisorConfig
    provider_environment: Mapping[str, str] = field(repr=False)

    @property
    def redaction_values(self) -> tuple[str, ...]:
        return tuple(self.provider_environment.values())


class ImageAgentConfigMaterializer:
    """Build complete, immutable, instance-local runtime revisions."""

    def __init__(self, store: FileStateStore, task_config: TaskConfigService) -> None:
        self.store = store
        self.task_config = task_config
        self.revisions = InstanceConfigRevisionStore(store)

    def materialize(self, task_id: str, instance_id: str) -> dict[str, Any]:
        """Return the active bundle, creating an initial v2 revision when absent."""

        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        current = self.revisions.read_current(task_id, instance_id)
        if current is None:
            materialization_lock = FileLock(
                self._materialization_lock_path(task_id, instance_id),
                self.store.lock_timeout_seconds,
            )
            try:
                materialization_lock.acquire()
            except HarnessError as exc:
                if exc.code != "REVISION_CONFLICT":
                    raise
                # A concurrent initializer may have committed the immutable
                # revision while this reader exhausted its bounded lock wait.
                current = self.revisions.read_current(task_id, instance_id)
                if current is None:
                    raise
            else:
                try:
                    current = self.revisions.read_current(task_id, instance_id)
                    if current is None:
                        built = self.build_revision(
                            task_id,
                            instance_id,
                            overrides={},
                            created_by={"type": "system", "id": "image_config_materializer"},
                            apply_mode="before_start",
                            apply_status="APPLIED",
                            confirmed_at=utc_now(),
                            effective_from_state="initial",
                        )
                        self.revisions.write_revision(
                            task_id,
                            instance_id,
                            built["manifest"],
                            built["runtime"],
                            built["model_config"],
                        )
                        self.revisions.set_current(
                            task_id,
                            instance_id,
                            built["manifest"]["revision_id"],
                            expected_revision=0,
                            updated_at=built["manifest"]["created_at"],
                        )
                        current = self.revisions.read_current(task_id, instance_id)
                finally:
                    materialization_lock.release()
        assert current is not None
        root = self.runtime_root(task_id, instance_id)
        if current.get("legacy"):
            runtime_path = root / "runtime.yaml"
            model_path = root / "model_config.yaml"
            config_root = None
        else:
            revision_root = root / "revisions" / current["manifest"]["revision_id"]
            runtime_path = revision_root / "runtime.yaml"
            model_path = revision_root / "model_config.yaml"
            config_root = root
        task_revision = self.task_config.revisions.read_revision(
            task_id,
            current["manifest"]["task_config_revision_id"],
        )
        return {
            "source_config_revision": task_revision["source_system_revision"],
            "task_config_revision_id": current["manifest"]["task_config_revision_id"],
            "runtime_config_revision_id": current["manifest"]["revision_id"],
            "config_hash": current["manifest"]["config_hash"],
            "runtime_path": runtime_path,
            "model_config_path": model_path,
            "config_root": config_root,
            "manifest": deepcopy(current["manifest"]),
            "state": deepcopy(current["state"]),
        }

    def build_revision(
        self,
        task_id: str,
        instance_id: str,
        *,
        overrides: dict[str, Any],
        created_by: dict[str, str],
        apply_mode: str,
        apply_status: str,
        confirmed_at: str | None,
        effective_from_state: str,
        revision_id: str | None = None,
        branch_id: str | None = None,
        checkpoint_id: str | None = None,
        require_current_approval: bool = False,
    ) -> dict[str, Any]:
        """Build, but do not publish, one complete v2 revision bundle."""

        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        current = self.revisions.read_current(task_id, instance_id)
        parent = None if current is None else str(current["manifest"]["revision_id"])
        if revision_id is None:
            sequence = 1 if parent is None else int(parent.rsplit("r", 1)[1]) + 1
            revision_id = f"cfg-inst-r{sequence:06d}"
        task_current = self.task_config.get_current(task_id)
        task_revision = task_current["revision"]
        clean_overrides = self.validate_overrides(
            task_revision,
            overrides,
            require_current_approval=require_current_approval,
        )
        effective = self.effective_runtime(task_revision, clean_overrides)
        # The v2 runtime document is the credential-safe editable projection.
        # The Image Agent derives managed-only invariants (offline_mode=False
        # and manual release) while loading the immutable revision.
        runtime = deepcopy(effective)
        model_config, bindings = self._model_configuration(
            task_revision,
            clean_overrides,
            revision_id,
            require_current_approval=require_current_approval,
        )
        created_at = utc_now()
        manifest = self.revisions.build_manifest(
            task_id=task_id,
            instance_id=instance_id,
            revision_id=revision_id,
            parent_revision_id=parent,
            task_config_revision_id=task_revision["revision_id"],
            overrides=clean_overrides,
            effective_runtime=effective,
            model_bindings=bindings,
            runtime=runtime,
            model_config=model_config,
            created_by=created_by,
            created_at=created_at,
            confirmed_at=confirmed_at,
            apply_mode=apply_mode,
            apply_status=apply_status,
            branch_id=branch_id,
            checkpoint_id=checkpoint_id,
            effective_from_state=effective_from_state,
        )
        self.store.contracts.validate("instance-runtime-config-manifest", manifest)
        return {
            "manifest": manifest,
            "runtime": runtime,
            "model_config": model_config,
        }

    def publish_revision(self, bundle: dict[str, Any]) -> dict[str, Any]:
        manifest = bundle["manifest"]
        return self.revisions.write_revision(
            manifest["task_id"],
            manifest["instance_id"],
            manifest,
            bundle["runtime"],
            bundle["model_config"],
        )

    def resolve_launch(self, task_id: str, instance_id: str) -> ImageAgentLaunchConfiguration:
        materialized = self.materialize(task_id, instance_id)
        snapshot = self.task_config.resolve_revision(
            task_id,
            materialized["task_config_revision_id"],
        )
        public = self.task_config.get_public(task_id)
        if public["source_config_revision"] != materialized["source_config_revision"]:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The Image Agent materialization does not match its task configuration.",
                {"instance_id": instance_id},
            )
        return ImageAgentLaunchConfiguration(
            source_config_revision=snapshot.revision,
            task_config_revision_id=materialized["task_config_revision_id"],
            runtime_config_revision_id=materialized["runtime_config_revision_id"],
            config_hash=materialized["config_hash"],
            runtime_path=materialized["runtime_path"],
            model_config_path=materialized["model_config_path"],
            config_root=materialized["config_root"],
            supervisor=snapshot.runtime.supervisor,
            provider_environment=self._provider_environment(snapshot),
        )

    def validate_overrides(
        self,
        task_revision: dict[str, Any],
        overrides: dict[str, Any],
        *,
        require_current_approval: bool,
    ) -> dict[str, Any]:
        """Reject fields, models, or Providers outside the safe control contract."""

        candidate = deepcopy(overrides)
        unknown = set(candidate) - set(RUNTIME_SETTING_FIELDS)
        if unknown:
            raise HarnessError(
                "FIELD_NOT_EDITABLE",
                "The runtime configuration contains a non-editable override.",
                {"fields": sorted(unknown)},
            )
        advanced = candidate.get("advanced_model_overrides") or {}
        if not isinstance(advanced, dict) or set(advanced) - set(MODEL_STATES):
            raise HarnessError(
                "FIELD_NOT_EDITABLE",
                "The runtime configuration contains an unknown model state.",
            )
        if any(not isinstance(value, str) or not value for value in advanced.values()):
            raise HarnessError(
                "VALIDATION_ERROR",
                "Advanced model overrides must reference non-empty model IDs.",
            )
        self_check_override = candidate.get("self_check") or {}
        if not isinstance(self_check_override, dict):
            raise HarnessError(
                "VALIDATION_ERROR",
                "The self-check override must be an object.",
            )
        if set(self_check_override) - {
            "termination",
            "fixed_rounds",
            "max_rounds",
            "stop_early_on_pass",
        }:
            raise HarnessError(
                "FIELD_NOT_EDITABLE",
                "The runtime configuration contains an unknown self-check field.",
            )
        probe_runtime = self.effective_runtime(task_revision, candidate)
        self._validate_effective_runtime(probe_runtime)
        if probe_runtime["self_check"]["fixed_rounds"] > probe_runtime["self_check"]["max_rounds"]:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The fixed self-check rounds cannot exceed the maximum rounds.",
            )
        self._model_configuration(
            task_revision,
            candidate,
            "cfg-inst-r000001",
            require_current_approval=require_current_approval,
        )
        return candidate

    @staticmethod
    def _validate_effective_runtime(runtime: dict[str, Any]) -> None:
        integer_ranges = {
            "max_auto_questions": (0, 10),
            "clarification_total_budget": (0, 100),
            "candidate_concurrency": (1, 5),
        }
        for name, (minimum, maximum) in integer_ranges.items():
            value = runtime.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise HarnessError("VALIDATION_ERROR", f"{name} is outside its safe range.")
        if runtime.get("question_preference") not in {"proactive", "blocking_only"}:
            raise HarnessError("VALIDATION_ERROR", "question_preference is invalid.")
        if runtime.get("response_format") not in {"url", "b64_json"}:
            raise HarnessError("VALIDATION_ERROR", "response_format is invalid.")
        if type(runtime.get("watermark")) is not bool:
            raise HarnessError("VALIDATION_ERROR", "watermark must be a boolean.")
        output_size = runtime.get("default_output_size")
        if not isinstance(output_size, str) or _OUTPUT_SIZE.fullmatch(output_size) is None:
            raise HarnessError("VALIDATION_ERROR", "default_output_size is invalid.")
        self_check = runtime.get("self_check")
        if not isinstance(self_check, dict) or self_check.get("termination") not in {"fix", "solo"}:
            raise HarnessError("VALIDATION_ERROR", "self_check.termination is invalid.")
        for name, maximum in (("fixed_rounds", 20), ("max_rounds", 50)):
            value = self_check.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise HarnessError("VALIDATION_ERROR", f"self_check.{name} is invalid.")
        if type(self_check.get("stop_early_on_pass")) is not bool:
            raise HarnessError(
                "VALIDATION_ERROR", "self_check.stop_early_on_pass must be a boolean."
            )

    @staticmethod
    def merge_overrides(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(current)
        for field_name, value in patch.items():
            if field_name in {"self_check", "advanced_model_overrides"}:
                if value is None:
                    merged.pop(field_name, None)
                    continue
                nested = dict(merged.get(field_name) or {})
                for nested_field, nested_value in value.items():
                    if nested_value is None:
                        nested.pop(nested_field, None)
                    else:
                        nested[nested_field] = nested_value
                if nested:
                    merged[field_name] = nested
                else:
                    merged.pop(field_name, None)
                continue
            if value is None:
                merged.pop(field_name, None)
            else:
                merged[field_name] = value
        return merged

    def model_options(self, task_revision: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        current = self.task_config.require_process_snapshot()
        approved = {
            item.id
            for group in (
                current.model_list.text_models,
                current.model_list.vlm_models,
                current.model_list.image_models,
            )
            for item in group
        }
        options: dict[str, list[dict[str, Any]]] = {}
        for state, _, group, _, capability in _STATE_ROUTES:
            options[state] = [
                {"id": item["id"], "label": item["label"]}
                for item in task_revision["model_list"][group]
                if item["id"] in approved
                and capability in item["capabilities"]
                and item["provider"] in current.providers.providers
            ]
        return options

    def runtime_root(self, task_id: str, instance_id: str) -> Path:
        return self.store.layout.initialize_instance(task_id, instance_id) / "runtime-config"

    def _materialization_lock_path(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "locks"
            / f"image-config-materialize-{task_id}-{instance_id}.lock"
        )

    @staticmethod
    def effective_runtime(
        task_revision: dict[str, Any], overrides: dict[str, Any]
    ) -> dict[str, Any]:
        image = task_revision["runtime"]["image_agent"]
        effective = {
            "question_preference": {
                "proactive": "proactive",
                "on_demand": "blocking_only",
                "blocking_only": "blocking_only",
            }[image["question_preference"]],
            "max_auto_questions": 3,
            "clarification_total_budget": 10,
            "candidate_concurrency": image["candidate_concurrency"],
            "default_output_size": image["default_output_size"],
            "response_format": image["response_format"],
            "watermark": image["watermark"],
            "self_check": deepcopy(_DEFAULT_SELF_CHECK),
        }
        for field_name, value in overrides.items():
            if field_name == "advanced_model_overrides":
                continue
            if field_name == "self_check":
                effective["self_check"].update(value)
            else:
                effective[field_name] = value
        return effective

    def _model_configuration(
        self,
        task_revision: dict[str, Any],
        overrides: dict[str, Any],
        revision_id: str,
        *,
        require_current_approval: bool,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        groups = {
            name: {item["id"]: item for item in task_revision["model_list"][name]}
            for name in ("text_models", "vlm_models", "image_models")
        }
        baseline = task_revision["runtime"]["image_agent"]["advanced_model_overrides"]
        requested = {
            **{name: value for name, value in baseline.items() if value is not None},
            **(overrides.get("advanced_model_overrides") or {}),
        }
        current = self.task_config.require_process_snapshot()
        currently_approved = {
            item.id
            for group in (
                current.model_list.text_models,
                current.model_list.vlm_models,
                current.model_list.image_models,
            )
            for item in group
        }
        bindings: dict[str, str] = {}
        documents: list[dict[str, Any]] = []
        for state, role, group, default_name, capability in _STATE_ROUTES:
            selected_id = requested.get(state) or task_revision["runtime"]["models"][default_name]
            model = groups[group].get(selected_id)
            if model is None or capability not in model["capabilities"]:
                raise HarnessError(
                    "MODEL_NOT_APPROVED",
                    "The selected model is not approved for this workflow state.",
                    {"state": state, "model_id": selected_id},
                )
            if require_current_approval and selected_id not in currently_approved:
                raise HarnessError(
                    "MODEL_NOT_APPROVED",
                    "The selected model is no longer in the system approval list.",
                    {"state": state, "model_id": selected_id},
                )
            if model["provider"] not in current.providers.providers:
                raise HarnessError(
                    "MODEL_PROVIDER_NOT_AUTHORIZED",
                    "The selected model Provider is not authorized for this instance.",
                    {"state": state, "model_id": selected_id},
                )
            # Manifest bindings describe the executable provider model, matching
            # the Image Agent's parsed StateBinding projection. The Harness-only
            # catalog ID remains the selection input and is not executable.
            bindings[state] = model["model"]
            documents.append(
                {
                    "state": state,
                    "model_role": role,
                    "provider": model["provider"],
                    "model": model["model"],
                    "parameters": dict(model["parameters"]),
                    "fallback_model": None,
                }
            )
        return {
            "model_config_id": f"materialized-{revision_id}",
            "state_bindings": documents,
        }, bindings

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
                    "MODEL_PROVIDER_NOT_AUTHORIZED",
                    "The task configuration references an unsupported Image Agent Provider.",
                    {"provider": name},
                ) from None
            environment[base_url_env] = provider.base_url
            environment[api_key_env] = provider.api_key.get_secret_value()
        return MappingProxyType(environment)


RUNTIME_SETTING_FIELDS = (
    "question_preference",
    "max_auto_questions",
    "clarification_total_budget",
    "candidate_concurrency",
    "default_output_size",
    "response_format",
    "watermark",
    "self_check",
    "advanced_model_overrides",
)
MODEL_STATES = _MODEL_STATES
