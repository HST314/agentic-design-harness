"""Internal process settings derived from the validated deployment snapshot."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config_kernel import ConfigSnapshot, load_config_snapshot


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
    image_agent_lock_path: Path = Path("agents/image-agent.lock.json")
    image_agent_path_mode: Literal["embedded_only", "external_only"] = "embedded_only"
    delivery_bundle_migration_mode: Literal[
        "legacy_only", "dual_write", "bundle_only"
    ] = "bundle_only"
    image_agent_python: Path = Field(default_factory=lambda: Path(sys.executable))
    image_agent_dependency_root: Path = Path(".runtime/image-agent-deps")
    image_agent_revision: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
    )
    master_gateway_url: str | None = None
    master_gateway_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    config_snapshot: ConfigSnapshot | None = Field(default=None, repr=False)

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
        if self.image_agent_path_mode == "embedded_only":
            selected_root = embedded_root
        elif self.image_agent_root != Path("agents/image_agent_mvp"):
            selected_root = configured_root
        else:
            raise ValueError(
                "external_only requires an explicit image_agent_root; "
                "automatic legacy-directory fallback was removed after P6 acceptance"
            )
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


def settings_from_snapshot(project_root: Path, snapshot: ConfigSnapshot) -> HarnessSettings:
    """Derive process settings without introducing a second configuration source."""

    server = snapshot.runtime.server
    return HarnessSettings(
        host=server.host,
        port=server.port,
        log_level=server.log_level,
        config_snapshot=snapshot,
    ).resolve_from(project_root)


def load_settings(
    project_root: Path, environ: Mapping[str, str] | None = None
) -> HarnessSettings:
    """Validate the root configuration and derive internal process settings."""

    return settings_from_snapshot(
        project_root, load_config_snapshot(project_root, environ)
    )
