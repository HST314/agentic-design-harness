"""Fail-closed deployment identity checks for the embedded Image Agent runtime."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

from ..core.errors import HarnessError
from ..storage.atomic import digest_json
from ..storage.repository import utc_now
from .image_lock import ImageAgentReleaseLock, runtime_platform_key
from .image_runtime import content_tree_sha256, dependency_tree_sha256


@dataclass(frozen=True, slots=True)
class RuntimeAttestation:
    """Stable runtime identity plus the time at which this process verified it."""

    builder_schema: str
    revision: str
    package_version: str
    contract_version: str
    source_sha256: str
    dependency_sha256: str
    dependency_lock_set_sha256: str
    platform: str
    identity_sha256: str
    verified_at: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


def attest_image_runtime(
    release_lock: ImageAgentReleaseLock,
    *,
    source_root: Path,
    dependency_root: Path,
    harness_root: Path,
) -> RuntimeAttestation:
    """Verify every runtime-relevant byte before the control plane becomes ready."""

    source = _safe_directory(source_root, "source")
    dependencies = _safe_directory(dependency_root, "dependency")
    harness = _safe_directory(harness_root, "Harness")
    try:
        source_sha256 = content_tree_sha256(source)
        dependency_sha256 = dependency_tree_sha256(dependencies)
        lock_set_sha256 = _dependency_lock_set_sha256(release_lock, harness, source)
    except HarnessError as exc:
        if exc.code == "IMAGE_RUNTIME_ATTESTATION_FAILED":
            raise
        raise HarnessError(
            "IMAGE_RUNTIME_ATTESTATION_FAILED",
            "The Image Agent deployment cannot be attested safely.",
            {"cause_code": exc.code},
        ) from None
    expected = {
        "source_sha256": release_lock.source_content_sha256,
        "dependency_sha256": release_lock.runtime_dependency_tree_sha256,
        "dependency_lock_set_sha256": release_lock.dependency_lock_set_sha256,
    }
    actual = {
        "source_sha256": source_sha256,
        "dependency_sha256": dependency_sha256,
        "dependency_lock_set_sha256": lock_set_sha256,
    }
    mismatches = {
        name: {"expected_sha256": expected[name], "actual_sha256": value}
        for name, value in actual.items()
        if value != expected[name]
    }
    if mismatches:
        raise HarnessError(
            "IMAGE_RUNTIME_ATTESTATION_FAILED",
            "The Image Agent deployment does not match its release lock.",
            {"mismatches": mismatches},
        )
    platform = runtime_platform_key()
    identity = {
        "builder_schema": "3.0",
        "revision": release_lock.revision,
        "package_version": release_lock.package_version,
        "contract_version": release_lock.contract_version,
        "source_sha256": source_sha256,
        "dependency_sha256": dependency_sha256,
        "dependency_lock_set_sha256": lock_set_sha256,
        "platform": platform,
    }
    return RuntimeAttestation(
        **identity,
        identity_sha256=digest_json(identity),
        verified_at=utc_now(),
    )


def _safe_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        _attestation_failed(f"The Image Agent {label} directory is missing.")
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        _attestation_failed(f"The Image Agent {label} path is not a safe directory.")
    return resolved


def _dependency_lock_set_sha256(
    release_lock: ImageAgentReleaseLock,
    harness_root: Path,
    source_root: Path,
) -> str:
    digest = hashlib.sha256()
    for item in release_lock.dependency_files:
        base = harness_root if item.scope == "harness" else source_root
        path = base / item.path
        content = _portable_regular_file(path)
        actual = hashlib.sha256(content).hexdigest()
        if actual != item.sha256:
            raise HarnessError(
                "IMAGE_RUNTIME_ATTESTATION_FAILED",
                "An Image Agent dependency lock file has drifted.",
                {
                    "scope": item.scope,
                    "dependency": item.path,
                    "expected_sha256": item.sha256,
                    "actual_sha256": actual,
                },
            )
        digest.update(f"{item.scope}:{item.path}".encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _portable_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise OSError("unsafe dependency lock file")
        content = path.read_bytes()
    except OSError:
        _attestation_failed("An Image Agent dependency lock file cannot be inspected.")
    if b"\0" in content:
        return content
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return content.replace(b"\r\n", b"\n")


def _attestation_failed(message: str) -> NoReturn:
    raise HarnessError("IMAGE_RUNTIME_ATTESTATION_FAILED", message)
