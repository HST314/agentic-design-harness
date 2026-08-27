"""Fail-closed identity proof for the isolated PPT Agent runtime."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import NoReturn

from ..core.errors import HarnessError
from ..runtime_identity import (
    RuntimeIdentityError,
    dependency_tree_sha256,
    inspect_ppt_runtime_packages,
    inspect_python_interpreter,
)
from ..storage.atomic import read_json
from .ppt_lock import PptAgentReleaseLock

PPT_DEPENDENCY_STAMP = ".requirements-installed.json"
_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)")


def attest_ppt_runtime(
    release_lock: PptAgentReleaseLock,
    *,
    source_root: Path,
    dependency_root: Path,
    harness_root: Path,
    interpreter: Path,
) -> str:
    """Return the installed tree digest only after every release input is proven."""

    source = _safe_directory(source_root, "source")
    dependencies = _safe_directory(dependency_root, "dependency")
    harness = _safe_directory(harness_root, "Harness")
    lock_set = _dependency_lock_set_sha256(release_lock, harness, source)
    if lock_set != release_lock.dependency_lock_set_sha256:
        _failed("The PPT Agent dependency lock set does not match its release lock.")
    expected_versions = _locked_versions(release_lock, harness, source)
    try:
        dependency_sha256 = dependency_tree_sha256(dependencies)
        interpreter_identity = inspect_python_interpreter([interpreter])
        packages = inspect_ppt_runtime_packages(
            interpreter,
            source_root=source,
            dependency_root=dependencies,
        )
        stamp = read_json(dependencies / PPT_DEPENDENCY_STAMP)
    except (RuntimeIdentityError, HarnessError, OSError) as exc:
        _failed(f"The PPT Agent dependency runtime cannot be proved: {exc}")
    if not _same_executable(interpreter_identity.executable, packages.python_executable):
        _failed("The PPT Agent import probe used an unexpected interpreter.")
    actual_versions = packages.distributions
    mismatches = {
        name: {"expected": version, "actual": actual_versions.get(name)}
        for name, version in expected_versions.items()
        if actual_versions.get(name) != version
    }
    unexpected = sorted(set(actual_versions) - set(expected_versions))
    if mismatches or unexpected:
        raise HarnessError(
            "PPT_RUNTIME_ATTESTATION_FAILED",
            "The PPT Agent installed distributions do not match the deterministic lock.",
            {"mismatches": mismatches, "unexpected": unexpected},
        )
    expected_stamp = {
        "schema_version": "1.0",
        "dependency_lock_set_sha256": lock_set,
        "dependency_sha256": dependency_sha256,
        "interpreter": {
            "implementation": interpreter_identity.implementation,
            "cache_tag": interpreter_identity.cache_tag,
            "version": interpreter_identity.version,
        },
    }
    if stamp != expected_stamp:
        _failed("The PPT Agent dependency proof is missing, stale, or inconsistent.")
    return dependency_sha256


def dependency_lock_set_sha256(
    release_lock: PptAgentReleaseLock, harness_root: Path, source_root: Path
) -> str:
    return _dependency_lock_set_sha256(release_lock, harness_root, source_root)


def locked_ppt_versions(
    release_lock: PptAgentReleaseLock, harness_root: Path, source_root: Path
) -> dict[str, str]:
    return _locked_versions(release_lock, harness_root, source_root)


def _dependency_lock_set_sha256(
    release_lock: PptAgentReleaseLock, harness_root: Path, source_root: Path
) -> str:
    digest = hashlib.sha256()
    for item in release_lock.dependency_files:
        base = harness_root if item.scope == "harness" else source_root
        content = _portable_regular_file(base / item.path)
        actual = hashlib.sha256(content).hexdigest()
        if actual != item.sha256:
            raise HarnessError(
                "PPT_RUNTIME_ATTESTATION_FAILED",
                "A PPT Agent dependency lock file has drifted.",
                {"scope": item.scope, "dependency": item.path},
            )
        digest.update(f"{item.scope}:{item.path}".encode())
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _locked_versions(
    release_lock: PptAgentReleaseLock, harness_root: Path, source_root: Path
) -> dict[str, str]:
    versions: dict[str, str] = {}
    for item in release_lock.dependency_files:
        if item.scope != "harness":
            continue
        for line in _portable_regular_file(harness_root / item.path).decode("utf-8").splitlines():
            match = _PIN.match(line.strip())
            if match is None:
                continue
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            if name in versions:
                _failed("The PPT Agent deterministic lock contains a duplicate package.")
            versions[name] = match.group(2)
    required = {"fastapi", "html5lib", "openai", "pydantic", "pyyaml", "tinycss2", "uvicorn"}
    if not required <= versions.keys():
        _failed("The PPT Agent deterministic lock omits a required package.")
    return versions


def _safe_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except OSError:
        _failed(f"The PPT Agent {label} directory is missing.")
    if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        _failed(f"The PPT Agent {label} path is not a safe directory.")
    return resolved


def _portable_regular_file(path: Path) -> bytes:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise OSError
        content = path.read_bytes()
    except OSError:
        _failed("A PPT Agent dependency lock file cannot be inspected.")
    if b"\0" not in content:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            content = content.replace(b"\r\n", b"\n")
    return content


def _same_executable(first: str, second: str) -> bool:
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _failed(message: str) -> NoReturn:
    raise HarnessError(
        "PPT_RUNTIME_ATTESTATION_FAILED",
        message,
        {"action": "Run scripts/dev.py setup --force, then scripts/dev.py doctor."},
    )
