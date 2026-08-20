"""Control-data and task-workspace directory ownership policy."""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..core.errors import HarnessError

IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")


def validate_identifier(value: str, label: str = "identifier") -> str:
    if not IDENTIFIER.fullmatch(value):
        raise HarnessError(
            "VALIDATION_ERROR",
            f"Invalid {label}.",
            {"field": label},
        )
    return value


class StateLayout:
    def __init__(self, control_root: Path, workspace_root: Path) -> None:
        self.control_root = control_root
        self.workspace_root = workspace_root

    def initialize(self) -> None:
        for directory in (
            self.control_root,
            self.control_root / "config",
            self.control_root / "secrets",
            self.control_root / "tasks",
            self.control_root / "indexes",
            self.control_root / "locks",
            self.control_root / "idempotency",
            self.workspace_root,
            self.workspace_root / "tasks",
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)

    def initialize_task(self, task_id: str) -> tuple[Path, Path]:
        validate_identifier(task_id, "task_id")
        control_task = self.control_root / "tasks" / task_id
        workspace_task = self.workspace_root / "tasks" / task_id
        control_children = (
            control_task,
            control_task / "stages",
            control_task / "instances",
            control_task / "approvals",
            control_task / "inbox",
        )
        workspace_children = (
            workspace_task,
            workspace_task / "inputs" / "original",
            workspace_task / "inputs" / "selected",
            workspace_task / "inputs" / "manifests",
            workspace_task / "resources" / "shared",
            workspace_task / "resources" / "manifests",
            workspace_task / "instances",
            workspace_task / "approvals",
        )
        for directory in (*control_children, *workspace_children):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        return control_task, workspace_task
