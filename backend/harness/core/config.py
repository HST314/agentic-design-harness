"""Validated runtime configuration with YAML and environment overlays."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class HarnessSettings(BaseModel):
    """Public, non-secret settings for a single Harness control process."""

    model_config = ConfigDict(extra="forbid")

    control_root: Path = Path("control-data")
    workspace_root: Path = Path("workspace")
    host: str = "127.0.0.1"
    port: int = Field(default=18080, ge=1, le=65535)
    log_level: str = "INFO"
    lock_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    contracts_root: Path = Path("contracts/v1")
    image_agent_root: Path = Path("../image_agent_mvp")
    image_agent_python: Path = Path("/usr/bin/python3")
    image_agent_dependency_root: Path = Path(".runtime/image-agent-deps")
    image_agent_revision: str = Field(
        default="2339550ab15ad05e0dde7f48e1386a5a1a0eb663",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    def resolve_from(self, project_root: Path) -> HarnessSettings:
        updates: dict[str, Any] = {}
        for name in (
            "control_root",
            "workspace_root",
            "contracts_root",
            "image_agent_root",
            "image_agent_python",
            "image_agent_dependency_root",
        ):
            value = getattr(self, name)
            updates[name] = value if value.is_absolute() else project_root / value
        return self.model_copy(update=updates)


_ENV_MAP = {
    "HARNESS_CONTROL_ROOT": "control_root",
    "HARNESS_WORKSPACE_ROOT": "workspace_root",
    "HARNESS_HOST": "host",
    "HARNESS_PORT": "port",
    "HARNESS_LOG_LEVEL": "log_level",
    "HARNESS_LOCK_TIMEOUT_SECONDS": "lock_timeout_seconds",
    "HARNESS_CONTRACTS_ROOT": "contracts_root",
    "HARNESS_IMAGE_AGENT_ROOT": "image_agent_root",
    "HARNESS_IMAGE_AGENT_PYTHON": "image_agent_python",
    "HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT": "image_agent_dependency_root",
    "HARNESS_IMAGE_AGENT_REVISION": "image_agent_revision",
}


def load_settings(project_root: Path, environ: dict[str, str] | None = None) -> HarnessSettings:
    """Load optional YAML, then overlay explicitly supported environment keys."""

    source = os.environ if environ is None else environ
    values: dict[str, Any] = {}
    config_path = source.get("HARNESS_CONFIG")
    if config_path:
        candidate = Path(config_path)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        loaded = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("Harness YAML configuration must be an object")
        values.update(loaded or {})
    for environment_name, field_name in _ENV_MAP.items():
        if environment_name in source:
            values[field_name] = source[environment_name]
    return HarnessSettings.model_validate(values).resolve_from(project_root)
