"""Fail-closed relative-path handling for task workspaces."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import NoReturn

from ..core.errors import HarnessError

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def normalized_relative_path(value: str) -> PurePosixPath:
    """Return a canonical POSIX path without accepting host-specific escapes."""

    if not value or "\x00" in value or "\\" in value or _WINDOWS_DRIVE.match(value):
        _outside(value)
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        _outside(value)
    return candidate


def resolve_task_path(
    task_root: Path,
    relative_path: str,
    *,
    allowed_prefixes: tuple[str, ...] | None = None,
    require_exists: bool = True,
) -> Path:
    """Resolve a task-relative path while rejecting symlinks at every component."""

    normalized = normalized_relative_path(relative_path)
    if allowed_prefixes:
        prefixes = tuple(normalized_relative_path(item).parts for item in allowed_prefixes)
        if not any(normalized.parts[: len(prefix)] == prefix for prefix in prefixes):
            _outside(relative_path)
    root = task_root.resolve(strict=True)
    current = root
    for part in normalized.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                _outside(relative_path)
            resolved = current.resolve(strict=True)
            if os.path.commonpath((str(root), str(resolved))) != str(root):
                _outside(relative_path)
    if require_exists and not current.exists():
        raise HarnessError(
            "ASSET_VALIDATION_FAILED",
            "The requested task file does not exist.",
            {"path": relative_path},
        )
    try:
        parent = current.parent.resolve(strict=True)
    except FileNotFoundError:
        _outside(relative_path)
    if os.path.commonpath((str(root), str(parent))) != str(root):
        _outside(relative_path)
    return current


def _outside(value: str) -> NoReturn:
    raise HarnessError(
        "PATH_OUTSIDE_TASK_ROOT",
        "The path is not a safe task-relative path.",
        {"path": value},
    )
