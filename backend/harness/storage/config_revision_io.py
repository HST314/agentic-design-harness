"""Durable primitives for immutable configuration revision records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NoReturn

import yaml

from ..core.errors import HarnessError
from .atomic import atomic_write_bytes, fsync_directory, set_permissions
from .safe_open import is_link_or_reparse, open_regular_readonly

CrashHook = Callable[[str], None]
MAX_CONFIG_FILE_BYTES = 4 * 1024 * 1024
MAX_CONFIG_DEPTH = 32
MAX_CONFIG_CONTAINER_ITEMS = 512
_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_TRAVERSAL = re.compile(r"(?:^|[/\\])\.\.(?:[/\\]|$)")
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:api_key|access_token|authorization|base_url|cookie|credential|endpoint|password|private_key|secret)(?:$|_)",
    re.IGNORECASE,
)
_CREDENTIAL_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{8,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----)",
    re.IGNORECASE,
)


def invoke_crash_hook(hook: CrashHook | None, checkpoint: str) -> None:
    if hook is not None:
        hook(checkpoint)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_public_config_tree(
    value: Any,
    key: str = "",
) -> None:
    """Reject secrets, Provider URLs, unsafe paths, and non-JSON YAML values."""

    _validate_config_tree(value, key, public=True, depth=0, seen=set())


def validate_config_tree_shape(value: Any, key: str = "") -> None:
    """Bound legacy YAML structure while retaining its read-only private values."""

    _validate_config_tree(value, key, public=False, depth=0, seen=set())


def _validate_config_tree(
    value: Any,
    key: str,
    *,
    public: bool,
    depth: int,
    seen: set[int],
) -> None:
    if depth > MAX_CONFIG_DEPTH:
        _invalid_public_value(key)
    if isinstance(value, dict):
        if len(value) > MAX_CONFIG_CONTAINER_ITEMS or id(value) in seen:
            _invalid_public_value(key)
        seen.add(id(value))
        for child_key, child_value in value.items():
            if not isinstance(child_key, str) or (
                public and _SENSITIVE_KEY.search(child_key)
            ):
                raise HarnessError(
                    "CONFIG_INTEGRITY_FAILED",
                    "Runtime configuration contains a sensitive field.",
                    {"field": str(child_key)[:64]},
                )
            _validate_config_tree(
                child_value,
                child_key,
                public=public,
                depth=depth + 1,
                seen=seen,
            )
        return
    if isinstance(value, list):
        if len(value) > MAX_CONFIG_CONTAINER_ITEMS or id(value) in seen:
            _invalid_public_value(key)
        seen.add(id(value))
        for item in value:
            _validate_config_tree(
                item,
                key,
                public=public,
                depth=depth + 1,
                seen=seen,
            )
        return
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        _invalid_public_value(key)
    if not isinstance(value, str):
        _invalid_public_value(key)
    if not value or not public:
        return
    if (
        _URL.match(value)
        or value.startswith("/")
        or _WINDOWS_ABSOLUTE.match(value)
        or _TRAVERSAL.search(value)
        or _CREDENTIAL_VALUE.search(value)
    ):
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "Runtime configuration contains a secret, Provider URL, or unsafe path.",
            {"field": key[:64]},
        )


def _invalid_public_value(key: str) -> NoReturn:
    raise HarnessError(
        "CONFIG_INTEGRITY_FAILED",
        "Runtime configuration contains a non-public value type.",
        {"field": key[:64]},
    )


def read_regular_bytes(path: Path, *, trusted_root: Path) -> bytes:
    try:
        descriptor = open_regular_readonly(path, trusted_root=trusted_root)
        with os.fdopen(descriptor, "rb") as stream:
            content = stream.read(MAX_CONFIG_FILE_BYTES + 1)
        if len(content) > MAX_CONFIG_FILE_BYTES:
            raise ValueError("configuration revision file is too large")
        return content
    except (OSError, ValueError) as exc:
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "A configuration revision file is missing or unsafe.",
            {"file": path.name},
        ) from exc


def read_json_object(path: Path, *, trusted_root: Path) -> dict[str, Any]:
    content = read_regular_bytes(path, trusted_root=trusted_root)
    return parse_json_object(content, filename=path.name)


def parse_json_object(content: bytes, *, filename: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (UnicodeError, ValueError) as exc:
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "A configuration revision JSON document is invalid.",
            {"file": filename},
        ) from exc
    if not isinstance(value, dict):
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "A configuration revision document must be an object.",
            {"file": filename},
        )
    return value


def parse_yaml_object(content: bytes, *, filename: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(content)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "A configuration revision YAML document is invalid.",
            {"file": filename},
        ) from exc
    if not isinstance(value, dict):
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "A configuration revision YAML document must be an object.",
            {"file": filename},
        )
    return value


def ensure_private_directory(path: Path) -> None:
    if path.exists() and (not path.is_dir() or is_link_or_reparse(path)):
        raise HarnessError(
            "PATH_OUTSIDE_TASK_ROOT",
            "A configuration revision directory is unsafe.",
            {"directory": path.name},
        )
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    set_permissions(path, 0o700)


def publish_immutable_directory(
    final_path: Path,
    files: Mapping[str, bytes],
    *,
    crash_hook: CrashHook | None = None,
) -> bool:
    """Publish a complete directory in one rename, or verify an identical replay."""

    ensure_private_directory(final_path.parent)
    if final_path.exists():
        _verify_existing_directory(final_path, files)
        return False
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{final_path.name}.",
            suffix=".tmp",
            dir=final_path.parent,
        )
    )
    try:
        for name, content in files.items():
            if Path(name).name != name:
                raise ValueError("revision filenames must not contain path separators")
            atomic_write_bytes(staging / name, content, mode=0o400)
        fsync_directory(staging)
        invoke_crash_hook(crash_hook, "after_revision_staged")
        set_permissions(staging, 0o500)
        try:
            os.replace(staging, final_path)
        except OSError:
            if not final_path.exists():
                raise
            _verify_existing_directory(final_path, files)
            _remove_temporary_path(staging)
            return False
        fsync_directory(final_path.parent)
        invoke_crash_hook(crash_hook, "after_revision_published")
        return True
    except Exception:
        _remove_temporary_path(staging)
        raise


def recover_temporary_paths(root: Path) -> list[str]:
    if not root.exists():
        return []
    if not root.is_dir() or is_link_or_reparse(root):
        raise HarnessError(
            "PATH_OUTSIDE_TASK_ROOT",
            "A configuration revision directory is unsafe.",
            {"directory": root.name},
        )
    removed: list[str] = []
    for path in sorted(root.iterdir()):
        if not path.name.startswith(".") or not path.name.endswith(".tmp"):
            continue
        _remove_temporary_path(path)
        removed.append(path.name)
    if removed:
        fsync_directory(root)
    return removed


def _verify_existing_directory(path: Path, files: Mapping[str, bytes]) -> None:
    if not path.is_dir() or is_link_or_reparse(path):
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "An immutable configuration revision path is unsafe.",
            {"revision_id": path.name},
        )
    actual_names = {item.name for item in path.iterdir()}
    if actual_names != set(files):
        raise HarnessError(
            "CONFIG_INTEGRITY_FAILED",
            "An immutable configuration revision has an unexpected file set.",
            {"revision_id": path.name},
        )
    for name, expected in files.items():
        actual = read_regular_bytes(path / name, trusted_root=path)
        if actual != expected:
            raise HarnessError(
                "SETTINGS_REVISION_CONFLICT",
                "An immutable configuration revision ID was reused with different content.",
                {"revision_id": path.name},
            )


def _remove_temporary_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink(missing_ok=True)
        return
    set_permissions(path, 0o700)
    for child in path.iterdir():
        if not child.is_symlink():
            set_permissions(child, 0o600 if child.is_file() else 0o700)
    shutil.rmtree(path)
