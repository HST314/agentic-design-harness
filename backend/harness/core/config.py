"""Validated runtime configuration with YAML and environment overlays."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

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
    image_agent_root: Path = Path("agents/image_agent_mvp")
    image_agent_legacy_root: Path = Path("../image_agent_mvp")
    image_agent_lock_path: Path = Path("agents/image-agent.lock.json")
    image_agent_path_mode: Literal[
        "prefer_embedded", "embedded_only", "external_only"
    ] = "prefer_embedded"
    delivery_bundle_migration_mode: Literal[
        "legacy_only", "dual_write", "bundle_only"
    ] = "legacy_only"
    image_agent_python: Path = Field(default_factory=lambda: Path(sys.executable))
    image_agent_dependency_root: Path = Path(".runtime/image-agent-deps")
    image_agent_revision: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    master_gateway_url: str | None = None
    master_gateway_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @field_validator("master_gateway_url")
    @classmethod
    def validate_master_gateway_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("master_gateway_url must be an HTTP(S) service root")
        return value.rstrip("/")

    def resolve_from(self, project_root: Path) -> HarnessSettings:
        updates: dict[str, Any] = {}
        for name in (
            "control_root",
            "workspace_root",
            "contracts_root",
            "image_agent_legacy_root",
            "image_agent_lock_path",
            "image_agent_python",
            "image_agent_dependency_root",
        ):
            value = getattr(self, name)
            updates[name] = value if value.is_absolute() else project_root / value
        configured_root = self.image_agent_root
        configured_root = (
            configured_root
            if configured_root.is_absolute()
            else project_root / configured_root
        )
        embedded_root = project_root / "agents" / "image_agent_mvp"
        legacy_root = updates["image_agent_legacy_root"]
        if self.image_agent_path_mode == "embedded_only":
            selected_root = embedded_root
        elif self.image_agent_path_mode == "external_only":
            selected_root = (
                configured_root
                if self.image_agent_root != Path("agents/image_agent_mvp")
                else legacy_root
            )
        elif embedded_root.is_dir():
            selected_root = embedded_root
        elif (
            self.image_agent_root != Path("agents/image_agent_mvp")
            and configured_root.is_dir()
        ):
            selected_root = configured_root
        elif legacy_root.is_dir():
            selected_root = legacy_root
        else:
            selected_root = embedded_root
        updates["image_agent_root"] = selected_root
        return self.model_copy(update=updates)

    @property
    def delivery_bundle_write_targets(self) -> tuple[bool, bool]:
        """Return (legacy, bundle) targets for the staged data migration."""

        return {
            "legacy_only": (True, False),
            "dual_write": (True, True),
            "bundle_only": (False, True),
        }[self.delivery_bundle_migration_mode]


_ENV_MAP = {
    "HARNESS_CONTROL_ROOT": "control_root",
    "HARNESS_WORKSPACE_ROOT": "workspace_root",
    "HARNESS_HOST": "host",
    "HARNESS_PORT": "port",
    "HARNESS_LOG_LEVEL": "log_level",
    "HARNESS_LOCK_TIMEOUT_SECONDS": "lock_timeout_seconds",
    "HARNESS_CONTRACTS_ROOT": "contracts_root",
    "HARNESS_IMAGE_AGENT_ROOT": "image_agent_root",
    "HARNESS_IMAGE_AGENT_LEGACY_ROOT": "image_agent_legacy_root",
    "HARNESS_IMAGE_AGENT_LOCK_PATH": "image_agent_lock_path",
    "HARNESS_IMAGE_AGENT_PATH_MODE": "image_agent_path_mode",
    "HARNESS_DELIVERY_BUNDLE_MIGRATION_MODE": "delivery_bundle_migration_mode",
    "HARNESS_IMAGE_AGENT_PYTHON": "image_agent_python",
    "HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT": "image_agent_dependency_root",
    "HARNESS_IMAGE_AGENT_REVISION": "image_agent_revision",
    "HARNESS_MASTER_GATEWAY_URL": "master_gateway_url",
    "HARNESS_MASTER_GATEWAY_TIMEOUT_SECONDS": "master_gateway_timeout_seconds",
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
