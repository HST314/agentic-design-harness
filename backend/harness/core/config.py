"""Internal process settings derived from the validated deployment snapshot."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
    general_agent_root: Path = Path("agents/general-agent")
    image_agent_root: Path = Path("agents/image_agent_mvp")
    image_agent_lock_path: Path = Path("agents/image-agent.lock.json")
    image_agent_python: Path = Field(default_factory=lambda: Path(sys.executable))
    image_agent_dependency_root: Path = Path(".runtime/image-agent-deps")
    ppt_agent_root: Path = Path("agents/ppt-agent")
    ppt_agent_lock_path: Path = Path("agents/ppt-agent.lock.json")
    ppt_agent_python: Path = Field(default_factory=lambda: Path(sys.executable))
    ppt_agent_dependency_root: Path = Path(".runtime/ppt-agent-deps")
    ppt_agent_runtime_policy: Path = Path("config/ppt_agent_runtime.yaml")
    ppt_agent_model_config: Path = Path("config/ppt_agent_model_config.yaml")
    config_snapshot: ConfigSnapshot | None = Field(default=None, repr=False)

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
            "image_agent_lock_path",
            "image_agent_python",
            "image_agent_dependency_root",
            "ppt_agent_lock_path",
            "ppt_agent_python",
            "ppt_agent_dependency_root",
            "ppt_agent_runtime_policy",
            "ppt_agent_model_config",
        ):
            value = getattr(self, name)
            updates[name] = value if value.is_absolute() else project_root / value
        updates["image_agent_root"] = (
            self.image_agent_root
            if self.image_agent_root.is_absolute()
            else project_root / self.image_agent_root
        )
        updates["general_agent_root"] = (
            self.general_agent_root
            if self.general_agent_root.is_absolute()
            else project_root / self.general_agent_root
        )
        updates["ppt_agent_root"] = (
            self.ppt_agent_root
            if self.ppt_agent_root.is_absolute()
            else project_root / self.ppt_agent_root
        )
        return self.model_copy(update=updates)


def settings_from_snapshot(project_root: Path, snapshot: ConfigSnapshot) -> HarnessSettings:
    """Derive process settings without introducing a second configuration source."""

    server = snapshot.runtime.server
    return HarnessSettings(
        host=server.host,
        port=server.port,
        log_level=server.log_level,
        config_snapshot=snapshot,
    ).resolve_from(project_root)


def load_settings(project_root: Path, environ: Mapping[str, str] | None = None) -> HarnessSettings:
    """Validate the root configuration and derive internal process settings."""

    return settings_from_snapshot(project_root, load_config_snapshot(project_root, environ))
