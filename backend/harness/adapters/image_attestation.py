"""Lightweight identity and import checks for the embedded Image Agent runtime."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NoReturn

from ..core.errors import HarnessError
from ..runtime_identity import (
    RuntimeIdentityError,
    inspect_python_interpreter,
    inspect_runtime_packages,
    runtime_platform_identity,
)
from ..storage.atomic import digest_json
from ..storage.repository import utc_now
from .image_lock import ImageAgentReleaseLock
from .image_runtime import content_tree_sha256, dependency_tree_sha256

_PROJECT_NAME = re.compile(r'^name\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_PROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


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
    package_name: str
    python_implementation: str
    python_cache_tag: str
    python_version: str
    python_executable: str
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
    interpreter: Path,
) -> RuntimeAttestation:
    """Verify release inputs and record the legal local dependency identity."""

    platform = runtime_platform_identity()
    source = _safe_directory(source_root, "source")
    dependencies = _safe_directory(dependency_root, "dependency")
    harness = _safe_directory(harness_root, "Harness")
    try:
        source_sha256 = content_tree_sha256(source)
        dependency_sha256 = dependency_tree_sha256(dependencies)
        lock_set_sha256 = _dependency_lock_set_sha256(release_lock, harness, source)
        package_name, package_version = _source_package_identity(source)
        expected_distributions = _locked_distribution_versions(
            release_lock, harness, source
        )
    except HarnessError as exc:
        if exc.code == "IMAGE_RUNTIME_ATTESTATION_FAILED":
            raise
        raise HarnessError(
            "IMAGE_RUNTIME_ATTESTATION_FAILED",
            "The Image Agent deployment cannot be attested safely.",
            {"cause_code": exc.code},
        ) from None
    expected_identity = {
        "source_sha256": release_lock.source_content_sha256,
        "dependency_lock_set_sha256": release_lock.dependency_lock_set_sha256,
        "package_name": "image-agent-mvp",
        "package_version": release_lock.package_version,
    }
    actual_identity = {
        "source_sha256": source_sha256,
        "dependency_lock_set_sha256": lock_set_sha256,
        "package_name": package_name,
        "package_version": package_version,
    }
    mismatches = {
        name: {"expected": expected_identity[name], "actual": value}
        for name, value in actual_identity.items()
        if value != expected_identity[name]
    }
    if mismatches:
        raise HarnessError(
            "IMAGE_RUNTIME_ATTESTATION_FAILED",
            "The Image Agent source identity does not match its release inputs.",
            {"platform": platform, "mismatches": mismatches},
        )
    try:
        interpreter_identity = inspect_python_interpreter([interpreter])
        package_identity = inspect_runtime_packages(
            interpreter,
            source_root=source,
            dependency_root=dependencies,
        )
    except RuntimeIdentityError as exc:
        raise HarnessError(
            "IMAGE_RUNTIME_ATTESTATION_FAILED",
            str(exc),
            {
                "platform": platform,
                "interpreter": str(interpreter),
                "action": "Run scripts/dev.py setup --force, then scripts/dev.py doctor.",
            },
        ) from None
    if not _same_executable(
        interpreter_identity.executable, package_identity.python_executable
    ):
        raise HarnessError(
            "IMAGE_RUNTIME_ATTESTATION_FAILED",
            "The Image Agent import probe ran with an unexpected interpreter.",
            {
                "platform": platform,
                "interpreter": interpreter_identity.as_dict(),
                "import_probe_executable": package_identity.python_executable,
                "action": "Run scripts/dev.py setup --force, then scripts/dev.py doctor.",
            },
        )
    mismatches = {}
    for distribution, expected_version in expected_distributions.items():
        actual_version = package_identity.distributions.get(distribution)
        if actual_version != expected_version:
            mismatches[f"distribution:{distribution}"] = {
                "expected": expected_version,
                "actual": actual_version,
            }
    if mismatches:
        raise HarnessError(
            "IMAGE_RUNTIME_ATTESTATION_FAILED",
            "The Image Agent package identity does not match its release inputs.",
            {
                "platform": platform,
                "python_cache_tag": interpreter_identity.cache_tag,
                "mismatches": mismatches,
                "action": "Run scripts/dev.py setup --force, then scripts/dev.py doctor.",
            },
        )
    identity = {
        "builder_schema": "3.1",
        "revision": release_lock.revision,
        "package_version": release_lock.package_version,
        "contract_version": release_lock.contract_version,
        "source_sha256": source_sha256,
        "dependency_sha256": dependency_sha256,
        "dependency_lock_set_sha256": lock_set_sha256,
        "platform": platform,
        "package_name": package_name,
        "python_implementation": interpreter_identity.implementation,
        "python_cache_tag": interpreter_identity.cache_tag,
    }
    return RuntimeAttestation(
        **identity,
        python_version=interpreter_identity.version,
        python_executable=interpreter_identity.executable,
        identity_sha256=digest_json(identity),
        verified_at=utc_now(),
    )


def _same_executable(first: str, second: str) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second)
    )


def _source_package_identity(source_root: Path) -> tuple[str, str]:
    try:
        content = (source_root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        _attestation_failed("The Image Agent package metadata cannot be inspected.")
    name = _PROJECT_NAME.search(content)
    version = _PROJECT_VERSION.search(content)
    if name is None or version is None:
        _attestation_failed("The Image Agent package metadata is invalid.")
    return name.group(1), version.group(1)


def _locked_distribution_versions(
    release_lock: ImageAgentReleaseLock,
    harness_root: Path,
    source_root: Path,
) -> dict[str, str]:
    required = {
        "fastapi",
        "httpx",
        "openai",
        "pillow",
        "portalocker",
        "pydantic",
        "pyyaml",
        "uvicorn",
    }
    versions: dict[str, str] = {}
    pattern = re.compile(r"^([A-Za-z0-9_.-]+)==([^;\s]+)")
    for item in release_lock.dependency_files:
        base = harness_root if item.scope == "harness" else source_root
        try:
            lines = (base / item.path).read_text(encoding="utf-8").splitlines()
        except OSError:
            _attestation_failed("An Image Agent dependency lock file cannot be inspected.")
        for line in lines:
            matched = pattern.match(line.strip())
            if matched is None:
                continue
            name = re.sub(r"[-_.]+", "-", matched.group(1)).lower()
            if name in required:
                versions[name] = matched.group(2)
    missing = sorted(required - set(versions))
    if missing:
        _attestation_failed(
            "The Image Agent dependency locks omit required package versions."
        )
    return versions


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
