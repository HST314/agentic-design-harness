"""Validated Image Agent release lock used by runtime and verification gates."""

from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlsplit

from ..core.errors import HarnessError

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^[1-9][0-9]*\.(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))?$")
_DEPENDENCY_SCOPES = frozenset({"harness", "image_agent"})
_RUNTIME_PLATFORMS = frozenset({"linux-x86_64", "windows-amd64"})


@dataclass(frozen=True, slots=True)
class LockedDependencyFile:
    scope: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ImageAgentReleaseLock:
    schema_version: str
    repository: str
    revision: str
    package_version: str
    contract_version: str
    embedded_path: str
    source_content_sha256: str
    dependency_files: tuple[LockedDependencyFile, ...]
    dependency_lock_set_sha256: str
    runtime_dependency_tree_sha256: str


def load_image_agent_lock(path: Path) -> ImageAgentReleaseLock:
    """Load one fail-closed lock; no runtime version facts live in adapter constants."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _invalid_lock("The Image Agent release lock is missing or invalid JSON.", path)
    root = _object(document, "lock")
    _exact_keys(
        root,
        {
            "schema_version",
            "agent",
            "repository",
            "revision",
            "package_version",
            "contract_version",
            "embedded_path",
            "source_content_sha256",
            "dependencies",
        },
        "lock",
        path,
    )
    if root["schema_version"] != "1.0" or root["agent"] != "image_agent_mvp":
        _invalid_lock("The Image Agent release lock identity is unsupported.", path)
    repository = _string(root["repository"], "repository", path)
    parsed = urlsplit(repository)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        _invalid_lock("The Image Agent repository URL is not an approved HTTPS origin.", path)
    revision = _matching(root["revision"], _REVISION, "revision", path)
    package_version = _matching(
        root["package_version"], _VERSION, "package_version", path
    )
    contract_version = _matching(
        root["contract_version"], _VERSION, "contract_version", path
    )
    embedded_path = _relative_path(root["embedded_path"], "embedded_path", path)
    source_content_sha256 = _matching(
        root["source_content_sha256"], _SHA256, "source_content_sha256", path
    )
    dependencies = _object(root["dependencies"], "dependencies", path)
    _exact_keys(
        dependencies,
        {
            "files",
            "lock_set_sha256",
            "runtime_tree_sha256",
            "runtime_tree_sha256_by_platform",
        },
        "dependencies",
        path,
    )
    platform_digests = _object(
        dependencies["runtime_tree_sha256_by_platform"],
        "runtime_tree_sha256_by_platform",
        path,
    )
    _exact_keys(
        platform_digests,
        set(_RUNTIME_PLATFORMS),
        "runtime_tree_sha256_by_platform",
        path,
    )
    validated_platform_digests = {
        key: _matching(value, _SHA256, f"runtime digest for {key}", path)
        for key, value in platform_digests.items()
    }
    legacy_runtime_digest = _matching(
        dependencies["runtime_tree_sha256"], _SHA256, "runtime_tree_sha256", path
    )
    if validated_platform_digests["linux-x86_64"] != legacy_runtime_digest:
        _invalid_lock("The legacy Image Agent runtime digest must match Linux.", path)
    files_value = dependencies["files"]
    if not isinstance(files_value, list) or not files_value:
        _invalid_lock("The Image Agent dependency file lock is empty.", path)
    files: list[LockedDependencyFile] = []
    identities: set[tuple[str, str]] = set()
    for index, value in enumerate(files_value):
        item = _object(value, f"dependencies.files[{index}]", path)
        _exact_keys(
            item,
            {"scope", "path", "sha256"},
            f"dependencies.files[{index}]",
            path,
        )
        scope = _string(item["scope"], "scope", path)
        if scope not in _DEPENDENCY_SCOPES:
            _invalid_lock("The Image Agent dependency scope is unsupported.", path)
        relative = _relative_path(item["path"], "dependency path", path)
        identity = (scope, relative)
        if identity in identities:
            _invalid_lock("The Image Agent dependency lock contains a duplicate file.", path)
        identities.add(identity)
        files.append(
            LockedDependencyFile(
                scope=scope,
                path=relative,
                sha256=_matching(item["sha256"], _SHA256, "dependency sha256", path),
            )
        )
    if tuple((item.scope, item.path) for item in files) != tuple(
        sorted((item.scope, item.path) for item in files)
    ):
        _invalid_lock("The Image Agent dependency file lock is not canonical.", path)
    return ImageAgentReleaseLock(
        schema_version="1.0",
        repository=repository,
        revision=revision,
        package_version=package_version,
        contract_version=contract_version,
        embedded_path=embedded_path,
        source_content_sha256=source_content_sha256,
        dependency_files=tuple(files),
        dependency_lock_set_sha256=_matching(
            dependencies["lock_set_sha256"], _SHA256, "lock_set_sha256", path
        ),
        runtime_dependency_tree_sha256=validated_platform_digests[
            runtime_platform_key(path=path)
        ],
    )


def default_image_agent_lock_path() -> Path:
    return Path(__file__).resolve().parents[3] / "agents" / "image-agent.lock.json"


def runtime_platform_key(
    *,
    system: str | None = None,
    machine: str | None = None,
    path: Path | None = None,
) -> str:
    """Return the supported, canonical runtime dependency platform key."""

    selected_system = (platform.system() if system is None else system).lower()
    selected_machine = (platform.machine() if machine is None else machine).lower()
    if selected_system == "linux" and selected_machine in {"amd64", "x86_64"}:
        return "linux-x86_64"
    if selected_system == "windows" and selected_machine in {"amd64", "x86_64"}:
        return "windows-amd64"
    _invalid_lock(
        "The Image Agent runtime platform is not represented by the release lock.", path
    )


def _object(value: Any, label: str, path: Path | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid_lock(f"The Image Agent {label} must be an object.", path)
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str, path: Path) -> None:
    if set(value) != expected:
        _invalid_lock(f"The Image Agent {label} fields do not match the lock contract.", path)


def _string(value: Any, label: str, path: Path) -> str:
    if not isinstance(value, str) or not value:
        _invalid_lock(f"The Image Agent {label} is invalid.", path)
    return value


def _matching(value: Any, pattern: re.Pattern[str], label: str, path: Path) -> str:
    text = _string(value, label, path)
    if pattern.fullmatch(text) is None:
        _invalid_lock(f"The Image Agent {label} is invalid.", path)
    return text


def _relative_path(value: Any, label: str, path: Path) -> str:
    text = _string(value, label, path)
    candidate = Path(text)
    if (
        candidate.is_absolute()
        or "\\" in text
        or not candidate.parts
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        _invalid_lock(f"The Image Agent {label} must be a normalized relative path.", path)
    return candidate.as_posix()


def _invalid_lock(message: str, path: Path | None) -> NoReturn:
    details = {} if path is None else {"lock_path": str(path)}
    raise HarnessError("ADAPTER_UNAVAILABLE", message, details)
