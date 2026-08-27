"""Strict, aggregated loading for the deployment configuration bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"
FIX_COMMAND = "python3 scripts/dev.py config-check"
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SIZE = re.compile(r"^[1-9][0-9]{1,4}x[1-9][0-9]{1,4}$")
_SENSITIVE_PARAMETER = re.compile(
    r"(^|[_-])(api[_-]?)?(key|token|secret|password|url|endpoint)($|[_-])",
    re.IGNORECASE,
)
_RESERVED_MODEL_PARAMETERS = {
    "messages",
    "model",
    "response_format",
    "stream",
    "tool_choice",
    "tools",
}

Capability = Literal[
    "structured_output",
    "tool_calling",
    "image_input",
    "text_to_image",
    "image_to_image",
]
ParameterValue = str | int | float | bool | None


def _yaml_sequence_as_tuple(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


CapabilitySequence = Annotated[
    tuple[Capability, ...], BeforeValidator(_yaml_sequence_as_tuple)
]


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProviderConnection(StrictConfigModel):
    base_url: str = Field(min_length=1)
    api_key: SecretStr

    @field_validator("base_url")
    @classmethod
    def validate_service_root(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "must be an HTTP(S) service root without credentials, query, or fragment"
            )
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("must use HTTPS unless the provider is on localhost")
        return value.rstrip("/")


class ProviderConfig(StrictConfigModel):
    schema_version: Literal["1.0"]
    providers: Mapping[str, ProviderConnection] = Field(min_length=1)

    @field_validator("providers")
    @classmethod
    def only_ark_is_supported(
        cls, value: Mapping[str, ProviderConnection]
    ) -> Mapping[str, ProviderConnection]:
        unsupported = sorted(set(value) - {"ark"})
        if unsupported:
            raise ValueError(
                "P0 only supports the ark provider; unsupported: " + ", ".join(unsupported)
            )
        if "ark" not in value:
            raise ValueError("P0 requires providers.ark")
        return MappingProxyType(dict(value))

    @field_serializer("providers")
    def serialize_providers(self, value: Mapping[str, ProviderConnection]) -> dict[str, Any]:
        return dict(value)


class ModelDefinition(StrictConfigModel):
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)
    provider: Literal["ark"]
    model: str = Field(min_length=1, max_length=200)
    capabilities: CapabilitySequence = Field(min_length=1)
    parameters: Mapping[str, ParameterValue]

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if _MODEL_ID.fullmatch(value) is None:
            raise ValueError("must be a stable identifier containing letters, digits, ., _, or -")
        return value

    @field_validator("capabilities")
    @classmethod
    def unique_capabilities(cls, value: tuple[Capability, ...]) -> tuple[Capability, ...]:
        if len(set(value)) != len(value):
            raise ValueError("must not contain duplicate capability values")
        return value

    @field_validator("parameters")
    @classmethod
    def reject_sensitive_parameters(
        cls, value: Mapping[str, ParameterValue]
    ) -> Mapping[str, ParameterValue]:
        rejected = sorted(name for name in value if _SENSITIVE_PARAMETER.search(name))
        if rejected:
            raise ValueError(
                "must not contain secret, credential, URL, or endpoint fields: "
                + ", ".join(rejected)
            )
        reserved = sorted(set(value) & _RESERVED_MODEL_PARAMETERS)
        if reserved:
            raise ValueError(
                "must not override request-owned fields: " + ", ".join(reserved)
            )
        return MappingProxyType(dict(value))

    @field_serializer("parameters")
    def serialize_parameters(
        self, value: Mapping[str, ParameterValue]
    ) -> dict[str, ParameterValue]:
        return dict(value)


class ModelListConfig(StrictConfigModel):
    schema_version: Literal["1.0"]
    text_models: Annotated[
        tuple[ModelDefinition, ...], BeforeValidator(_yaml_sequence_as_tuple)
    ] = Field(min_length=1)
    vlm_models: Annotated[
        tuple[ModelDefinition, ...], BeforeValidator(_yaml_sequence_as_tuple)
    ] = Field(min_length=1)
    image_models: Annotated[
        tuple[ModelDefinition, ...], BeforeValidator(_yaml_sequence_as_tuple)
    ] = Field(min_length=1)


class ServerConfig(StrictConfigModel):
    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class RuntimeModelSelection(StrictConfigModel):
    master: str = Field(min_length=1)
    text_reasoning: str = Field(min_length=1)
    vision_understanding: str = Field(min_length=1)
    image_generation: str = Field(min_length=1)


class MasterConfig(StrictConfigModel):
    model_timeout_seconds: int = Field(ge=1, le=3600)
    max_tool_rounds: int = Field(ge=1, le=100)
    max_clarification_questions: int = Field(ge=0, le=20)
    require_plan_confirmation: bool


class DocumentProcessingConfig(StrictConfigModel):
    max_files_per_task: int = Field(ge=1, le=1000)
    max_total_bytes: int = Field(ge=1)
    max_pdf_pages: int = Field(ge=1, le=10000)
    text_chunk_chars: int = Field(ge=100, le=1_000_000)
    visual_analysis: Literal["auto", "always", "never"]
    require_source_citations: bool


class AdvancedModelOverrides(StrictConfigModel):
    intake_clarify: str | None
    confirmation_build: str | None
    initial_candidate_generation: str | None
    self_check_inspection: str | None
    self_check_rework: str | None
    human_prompt_rework: str | None


class LibraryReleaseConfig(StrictConfigModel):
    release: Literal["auto", "manual", "off"]


class ImageAgentSelfCheckConfig(StrictConfigModel):
    termination: Literal["fix", "solo"]
    fixed_rounds: int = Field(ge=1, le=20)
    max_rounds: int = Field(ge=1, le=50)
    stop_early_on_pass: bool

    @model_validator(mode="after")
    def ordered_rounds(self) -> ImageAgentSelfCheckConfig:
        if self.fixed_rounds > self.max_rounds:
            raise ValueError("fixed_rounds must not exceed max_rounds")
        return self


class ImageAgentConfig(StrictConfigModel):
    question_preference: Literal["proactive", "blocking_only", "on_demand"]
    max_auto_questions: int = Field(ge=0, le=10)
    clarification_total_budget: int = Field(ge=0, le=100)
    category_constraint: LibraryReleaseConfig
    style_direction: LibraryReleaseConfig
    candidate_concurrency: int = Field(ge=1, le=5)
    default_output_size: str
    response_format: Literal["url", "b64_json"]
    watermark: bool
    self_check: ImageAgentSelfCheckConfig
    advanced_model_overrides: AdvancedModelOverrides

    @field_validator("default_output_size")
    @classmethod
    def valid_output_size(cls, value: str) -> str:
        if _SIZE.fullmatch(value) is None:
            raise ValueError("must use WIDTHxHEIGHT with positive integer dimensions")
        return value


class ImageAgentRuntimeFileConfig(ImageAgentConfig):
    schema_version: Literal["1.0"]


class SupervisorConfig(StrictConfigModel):
    port_range_start: int = Field(ge=1, le=65535)
    port_range_end: int = Field(ge=1, le=65535)
    startup_timeout_seconds: int = Field(ge=1, le=3600)
    shutdown_grace_seconds: int = Field(ge=0, le=3600)
    probe_timeout_seconds: float = Field(default=2.0, gt=0, le=60)
    health_failure_threshold: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def ordered_port_range(self) -> SupervisorConfig:
        if self.port_range_start > self.port_range_end:
            raise ValueError("port_range_start must not exceed port_range_end")
        return self


class RuntimeFileConfig(StrictConfigModel):
    schema_version: Literal["1.0"]
    server: ServerConfig
    models: RuntimeModelSelection
    master: MasterConfig
    document_processing: DocumentProcessingConfig
    supervisor: SupervisorConfig

    @model_validator(mode="after")
    def distinct_server_and_supervisor_ports(self) -> RuntimeFileConfig:
        if self.supervisor.port_range_start <= self.server.port <= self.supervisor.port_range_end:
            raise ValueError("server.port must be outside supervisor.port_range_start/end")
        return self


class RuntimeConfig(RuntimeFileConfig):
    """Validated in-memory aggregate of Harness and Image Agent settings."""

    image_agent: ImageAgentConfig


class ConfigSnapshot(StrictConfigModel):
    """Immutable, validated configuration used for a task or process lifetime."""

    schema_version: Literal["1.0"]
    revision: str
    providers: ProviderConfig
    model_list: ModelListConfig
    runtime: RuntimeConfig


@dataclass(frozen=True, slots=True)
class ConfigProblem:
    filename: str
    path: str
    reason: str

    def render(self) -> str:
        location = f"{self.filename}: {self.path}" if self.path else self.filename
        return f"- {location} -> {self.reason}"


class ConfigurationError(ValueError):
    """All independently discoverable configuration problems from one load."""

    def __init__(self, problems: list[ConfigProblem]):
        self.problems = tuple(problems)
        super().__init__(self.render())

    def render(self) -> str:
        count = len(self.problems)
        noun = "error" if count == 1 else "errors"
        lines = [f"CONFIG_ERROR: configuration is incomplete ({count} {noun})"]
        lines.extend(problem.render() for problem in self.problems)
        lines.append(f"Fix the listed values and run: {FIX_COMMAND}")
        return "\n".join(lines)


def _error_path(location: tuple[str | int, ...]) -> str:
    return ".".join(str(part) for part in location) or "<root>"


def _validation_problems(filename: str, error: ValidationError) -> list[ConfigProblem]:
    return [
        ConfigProblem(filename, _error_path(item["loc"]), item["msg"])
        for item in error.errors(include_url=False, include_input=False)
    ]


def _yaml_problem(filename: str, error: yaml.YAMLError) -> ConfigProblem:
    mark = getattr(error, "problem_mark", None)
    path = (
        f"line {mark.line + 1}, column {mark.column + 1}"
        if mark is not None
        else "<document>"
    )
    reason = getattr(error, "problem", None) or str(error).splitlines()[0]
    return ConfigProblem(filename, path, f"invalid YAML: {reason}")


def _parse_env(path: Path, problems: list[ConfigProblem]) -> dict[str, str]:
    if not path.is_file():
        problems.append(ConfigProblem(path.name, "<file>", "required file is missing"))
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        problems.append(ConfigProblem(path.name, "<file>", f"cannot be read: {exc}"))
        return values
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            problems.append(
                ConfigProblem(
                    path.name,
                    f"line {number}",
                    "expected NAME=value without shell syntax",
                )
            )
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        value = raw_value.strip()
        if _ENV_NAME.fullmatch(name) is None:
            problems.append(ConfigProblem(path.name, f"line {number}", "invalid variable name"))
            continue
        if name in values:
            problems.append(
                ConfigProblem(path.name, f"line {number}", f'duplicate variable "{name}"')
            )
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if any(token in value for token in ("${", "$(`", "$(", "`")):
            problems.append(
                ConfigProblem(
                    path.name,
                    f"line {number}",
                    "shell expansion, command substitution, and nested variables are forbidden",
                )
            )
            continue
        values[name] = value
    return values


def _load_yaml(path: Path, problems: list[ConfigProblem]) -> Any | None:
    if not path.is_file():
        problems.append(ConfigProblem(path.name, "<file>", "required file is missing"))
        return None
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        problems.append(_yaml_problem(path.name, exc))
        return None
    except (OSError, UnicodeError) as exc:
        problems.append(ConfigProblem(path.name, "<file>", f"cannot be read: {exc}"))
        return None
    if not isinstance(value, dict):
        problems.append(ConfigProblem(path.name, "<root>", "must be a YAML object"))
        return None
    return value


def _find_env_references(value: Any, path: tuple[str | int, ...] = ()) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.extend(_find_env_references(child, (*path, str(key))))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_find_env_references(child, (*path, index)))
    elif isinstance(value, str) and "${" in value:
        found.append((_error_path(path), value))
    return found


def _substitute_provider_env(
    value: Any,
    environment: Mapping[str, str],
    problems: list[ConfigProblem],
    path: tuple[str | int, ...] = (),
) -> Any:
    if isinstance(value, dict):
        return {
            key: _substitute_provider_env(child, environment, problems, (*path, str(key)))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _substitute_provider_env(child, environment, problems, (*path, index))
            for index, child in enumerate(value)
        ]
    if not isinstance(value, str) or "${" not in value:
        return value
    matched = _ENV_REFERENCE.fullmatch(value)
    field_path = _error_path(path)
    if matched is None:
        problems.append(
            ConfigProblem(
                "provider.yaml",
                field_path,
                "environment values must use one complete ${ENV_NAME} replacement",
            )
        )
        return "missing" if path[-1:] == ("api_key",) else "https://missing.invalid"
    name = matched.group(1)
    if name not in environment or environment[name] == "":
        problems.append(
            ConfigProblem(
                "provider.yaml",
                field_path,
                f"environment variable {name} is missing",
            )
        )
        return "missing" if path[-1:] == ("api_key",) else "https://missing.invalid"
    return environment[name]


def _snapshot_revision(
    providers: ProviderConfig, model_list: ModelListConfig, runtime: RuntimeConfig
) -> str:
    provider_values = {
        name: {
            "base_url": provider.base_url,
            "api_key_sha256": hashlib.sha256(
                provider.api_key.get_secret_value().encode("utf-8")
            ).hexdigest(),
        }
        for name, provider in providers.providers.items()
    }
    payload = {
        "providers": provider_values,
        "model_list": model_list.model_dump(mode="json"),
        "runtime": runtime.model_dump(mode="json"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "cfg_" + hashlib.sha256(encoded).hexdigest()[:24]


class ConfigValidator:
    """Perform cross-file checks after all individual schemas have parsed."""

    _references = (
        ("models.master", "text_models", frozenset({"structured_output", "tool_calling"})),
        ("models.text_reasoning", "text_models", frozenset({"structured_output"})),
        (
            "models.vision_understanding",
            "vlm_models",
            frozenset({"image_input", "structured_output"}),
        ),
        ("models.image_generation", "image_models", frozenset({"text_to_image"})),
        (
            "image_agent.advanced_model_overrides.intake_clarify",
            "text_models",
            frozenset({"structured_output"}),
        ),
        (
            "image_agent.advanced_model_overrides.confirmation_build",
            "text_models",
            frozenset({"structured_output"}),
        ),
        (
            "image_agent.advanced_model_overrides.initial_candidate_generation",
            "image_models",
            frozenset({"text_to_image"}),
        ),
        (
            "image_agent.advanced_model_overrides.self_check_inspection",
            "vlm_models",
            frozenset({"image_input"}),
        ),
        (
            "image_agent.advanced_model_overrides.self_check_rework",
            "image_models",
            frozenset({"text_to_image"}),
        ),
        (
            "image_agent.advanced_model_overrides.human_prompt_rework",
            "image_models",
            frozenset({"text_to_image"}),
        ),
    )

    def validate(
        self,
        providers: ProviderConfig | None,
        model_list: ModelListConfig,
        runtime: RuntimeConfig,
    ) -> list[ConfigProblem]:
        problems: list[ConfigProblem] = []
        group_sequences = {
            "text_models": model_list.text_models,
            "vlm_models": model_list.vlm_models,
            "image_models": model_list.image_models,
        }
        groups = {
            group_name: {item.id: item for item in models}
            for group_name, models in group_sequences.items()
        }
        seen: dict[str, str] = {}
        for group_name, models in group_sequences.items():
            for index, model in enumerate(models):
                model_id = model.id
                if model_id in seen:
                    problems.append(
                        ConfigProblem(
                            "model_list.yaml",
                            f"{group_name}.{index}.id",
                            f'is duplicated in {seen[model_id]}; model ids must be globally unique',
                        )
                    )
                else:
                    seen[model_id] = group_name
                if providers is not None and model.provider not in providers.providers:
                    problems.append(
                        ConfigProblem(
                            "model_list.yaml",
                            f"{group_name}.{model_id}.provider",
                            f'unknown provider "{model.provider}"',
                        )
                    )

        all_categories = {
            model_id: group_name for group_name, models in groups.items() for model_id in models
        }
        for field_path, expected_group, required in self._references:
            model_id = self._runtime_reference(runtime, field_path)
            if model_id is None:
                continue
            actual_group = all_categories.get(model_id)
            if actual_group is None:
                problems.append(
                    ConfigProblem(
                        "runtime.yaml",
                        field_path,
                        f'unknown {expected_group.removesuffix("_models")} model id "{model_id}"',
                    )
                )
                continue
            if actual_group != expected_group:
                problems.append(
                    ConfigProblem(
                        "runtime.yaml",
                        field_path,
                        f'model "{model_id}" belongs to {actual_group}, expected {expected_group}',
                    )
                )
                continue
            model = groups[expected_group][model_id]
            missing = sorted(required - set(model.capabilities))
            if missing:
                problems.append(
                    ConfigProblem(
                        "runtime.yaml",
                        field_path,
                        f'model "{model_id}" lacks required capabilities: {", ".join(missing)}',
                    )
                )
        return problems

    @staticmethod
    def _runtime_reference(runtime: RuntimeConfig, path: str) -> str | None:
        value: Any = runtime
        for part in path.split("."):
            value = getattr(value, part)
        return value


class ConfigLoader:
    """Load, aggregate, and validate all deployment configuration files."""

    def __init__(self, project_root: Path, environ: Mapping[str, str] | None = None):
        self.project_root = project_root
        self.environ = dict(os.environ if environ is None else environ)

    def load(self) -> ConfigSnapshot:
        problems: list[ConfigProblem] = []
        dotenv = _parse_env(self.project_root / ".env", problems)
        environment = {**dotenv, **self.environ}
        config_root = self.project_root / "config"
        raw_provider = _load_yaml(config_root / "provider.yaml", problems)
        raw_models = _load_yaml(config_root / "model_list.yaml", problems)
        raw_runtime = _load_yaml(config_root / "runtime.yaml", problems)
        raw_image_runtime = _load_yaml(
            config_root / "image_agent_runtime.yaml", problems
        )

        if isinstance(raw_provider, dict):
            api_key = raw_provider.get("providers", {}).get("ark", {}).get("api_key")
            if not isinstance(api_key, str) or _ENV_REFERENCE.fullmatch(api_key) is None:
                problems.append(
                    ConfigProblem(
                        "provider.yaml",
                        "providers.ark.api_key",
                        "must be one complete ${ENV_NAME} reference; "
                        "plaintext secrets are forbidden",
                    )
                )
            raw_provider = _substitute_provider_env(raw_provider, environment, problems)
        for filename, value in (
            ("model_list.yaml", raw_models),
            ("runtime.yaml", raw_runtime),
            ("image_agent_runtime.yaml", raw_image_runtime),
        ):
            if value is not None:
                for field_path, _ in _find_env_references(value):
                    problems.append(
                        ConfigProblem(
                            filename,
                            field_path,
                            "environment substitution is only allowed in provider.yaml",
                        )
                    )

        provider_config = self._validate_schema(
            "provider.yaml", ProviderConfig, raw_provider, problems
        )
        model_config = self._validate_schema(
            "model_list.yaml", ModelListConfig, raw_models, problems
        )
        runtime_file_config = self._validate_schema(
            "runtime.yaml", RuntimeFileConfig, raw_runtime, problems
        )
        image_runtime_config = self._validate_schema(
            "image_agent_runtime.yaml",
            ImageAgentRuntimeFileConfig,
            raw_image_runtime,
            problems,
        )
        runtime_config = None
        if runtime_file_config is not None and image_runtime_config is not None:
            runtime_config = RuntimeConfig.model_validate(
                {
                    **runtime_file_config.model_dump(mode="json"),
                    "image_agent": image_runtime_config.model_dump(
                        mode="json", exclude={"schema_version"}
                    ),
                }
            )
        if model_config and runtime_config:
            problems.extend(
                ConfigValidator().validate(provider_config, model_config, runtime_config)
            )
        if problems:
            raise ConfigurationError(problems)
        assert provider_config is not None
        assert model_config is not None
        assert runtime_config is not None
        return ConfigSnapshot(
            schema_version=SCHEMA_VERSION,
            revision=_snapshot_revision(provider_config, model_config, runtime_config),
            providers=provider_config,
            model_list=model_config,
            runtime=runtime_config,
        )

    @staticmethod
    def _validate_schema(
        filename: str,
        schema: type[StrictConfigModel],
        value: Any | None,
        problems: list[ConfigProblem],
    ) -> Any | None:
        if value is None:
            return None
        try:
            return schema.model_validate(value)
        except ValidationError as exc:
            problems.extend(_validation_problems(filename, exc))
            return None


def load_config_snapshot(
    project_root: Path, environ: Mapping[str, str] | None = None
) -> ConfigSnapshot:
    return ConfigLoader(project_root, environ).load()


def build_config_snapshot(
    *,
    providers: ProviderConfig,
    model_list: ModelListConfig,
    runtime: RuntimeFileConfig | Mapping[str, Any],
    image_agent_runtime: ImageAgentRuntimeFileConfig | Mapping[str, Any],
) -> ConfigSnapshot:
    """Validate editable documents and build the same immutable process snapshot."""

    problems: list[ConfigProblem] = []
    try:
        runtime_file = (
            runtime
            if isinstance(runtime, RuntimeFileConfig)
            else RuntimeFileConfig.model_validate(runtime)
        )
    except ValidationError as exc:
        problems.extend(_validation_problems("runtime.yaml", exc))
        runtime_file = None
    try:
        image_file = (
            image_agent_runtime
            if isinstance(image_agent_runtime, ImageAgentRuntimeFileConfig)
            else ImageAgentRuntimeFileConfig.model_validate(image_agent_runtime)
        )
    except ValidationError as exc:
        problems.extend(_validation_problems("image_agent_runtime.yaml", exc))
        image_file = None
    aggregate = None
    if runtime_file is not None and image_file is not None:
        aggregate = RuntimeConfig.model_validate(
            {
                **runtime_file.model_dump(mode="json"),
                "image_agent": image_file.model_dump(
                    mode="json", exclude={"schema_version"}
                ),
            }
        )
        problems.extend(ConfigValidator().validate(providers, model_list, aggregate))
    if problems:
        raise ConfigurationError(problems)
    assert aggregate is not None
    return ConfigSnapshot(
        schema_version=SCHEMA_VERSION,
        revision=_snapshot_revision(providers, model_list, aggregate),
        providers=providers,
        model_list=model_list,
        runtime=aggregate,
    )
