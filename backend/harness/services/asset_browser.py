"""Committed-event allowlisting for task asset browser reads."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from ..core.errors import HarnessError
from ..storage.atomic import read_json
from ..storage.paths import normalized_relative_path, resolve_task_path
from ..storage.safe_open import is_link_or_reparse

ManifestVerifier = Callable[[str, dict[str, Any]], None]


def browser_roots(workspace: Path, group: str) -> list[Path]:
    roots: list[Path] = []
    if group in {"inputs", "all"}:
        roots.extend(
            workspace / item
            for item in ("inputs/original", "inputs/selected", "inputs/manifests")
        )
    if group in {"shared", "all"}:
        roots.extend(
            workspace / item for item in ("resources/shared", "resources/manifests")
        )
    if group in {"instances", "all"}:
        instances = workspace / "instances"
        for instance_root in sorted(instances.iterdir() if instances.exists() else []):
            if is_link_or_reparse(instance_root):
                raise HarnessError(
                    "PATH_OUTSIDE_TASK_ROOT",
                    "Linked instance resources are not browseable.",
                )
            if instance_root.is_dir():
                roots.append(instance_root / "outputs")
    return [root.resolve(strict=True) for root in roots]


def resolve_committed_browser_path(
    workspace: Path,
    relative_path: str,
    events: list[dict[str, Any]],
    verify_manifest: ManifestVerifier,
) -> Path:
    normalized, event = committed_browser_event(relative_path, events)
    return verify_browser_event_path(
        workspace, normalized.as_posix(), event, verify_manifest
    )


def committed_browser_event(
    relative_path: str,
    events: list[dict[str, Any]],
) -> tuple[PurePosixPath, dict[str, Any]]:
    normalized = normalized_relative_path(relative_path)
    fixed = {
        ("inputs", "original"),
        ("inputs", "selected"),
        ("inputs", "manifests"),
        ("resources", "shared"),
        ("resources", "manifests"),
    }
    parts = normalized.parts
    instance_output = (
        len(parts) >= 4 and parts[0] == "instances" and parts[2] == "outputs"
    )
    if parts[:2] not in fixed and not instance_output:
        raise HarnessError(
            "PATH_OUTSIDE_TASK_ROOT",
            "Only registered resources and instance outputs are browseable.",
            {"path": relative_path},
        )
    event = committed_browser_paths(events).get(normalized.as_posix())
    if event is None:
        raise HarnessError(
            "ASSET_VALIDATION_FAILED", "Only committed assets may be browsed."
        )
    return normalized, event


def committed_browser_paths(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    paths: dict[str, dict[str, Any]] = {}
    for event in events:
        paths[event["manifest"]["relative_path"]] = event
        paths[event["manifest_relpath"]] = event
    return paths


def verify_browser_event_path(
    workspace: Path,
    relative_path: str,
    event: dict[str, Any],
    verify_manifest: ManifestVerifier,
    *,
    verify_asset: bool = True,
) -> Path:
    manifest = event["manifest"]
    if verify_asset:
        verify_manifest(manifest["task_id"], manifest)
    try:
        path = resolve_task_path(workspace, relative_path)
    except HarnessError as exc:
        if exc.code == "ASSET_VALIDATION_FAILED":
            _corrupted(manifest["asset_id"])
        raise
    if path.is_symlink() or not path.is_file():
        _corrupted(manifest["asset_id"])
    if relative_path == event["manifest_relpath"]:
        try:
            if read_json(path) != manifest:
                _corrupted(manifest["asset_id"])
        except (OSError, ValueError, json.JSONDecodeError):
            _corrupted(manifest["asset_id"])
    return path


def _corrupted(asset_id: str) -> None:
    raise HarnessError(
        "ASSET_CORRUPTED",
        "The asset no longer matches its committed manifest.",
        {"asset_id": asset_id},
    )
