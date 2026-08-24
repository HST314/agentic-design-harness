"""Immutable runtime artifact construction for the Image Agent adapter."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Collection
from pathlib import Path

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, digest_json, fsync_directory, read_json
from ..storage.locks import FileLock

IMAGE_ENTRYPOINT = "_harness_image_server.py"
IMAGE_WEB_REQUIREMENTS = "_harness-image-web.in"

_MARKER = ".harness-runtime-artifact.json"
_IGNORED_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".requirements-installed",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "htmlcov",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)
_IGNORED_SUFFIXES = (".egg-info", ".pyc", ".pyo")
_DEPENDENCY_IGNORED_ROOT_NAMES = frozenset({"bin"})
_DEPENDENCY_IGNORED_NAMES = frozenset({"RECORD"})
_SHA256_LENGTH = 64
_SERVER_SOURCE = """\
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    uvicorn.run("main_front:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
"""
_WEB_REQUIREMENTS_SOURCE = """\
# The Image Agent exposes a FastAPI app but deliberately does not bundle an ASGI runner.
# Minimal non-reloading server; satisfies Image Agent requirements-front.txt.
uvicorn==0.35.0
"""


class ImageRuntimeBuilder:
    """Copy pinned Image code and dependencies into one read-only instance root."""

    def __init__(
        self,
        source_root: Path,
        dependency_root: Path,
        *,
        revision: str,
        package_version: str,
        source_content_sha256: str,
        dependency_content_sha256: str,
        identity_sha256: str | None = None,
        platform: str = "unknown",
    ) -> None:
        self.source_root = source_root
        self.dependency_root = dependency_root
        self.revision = revision
        self.package_version = package_version
        self.source_content_sha256 = source_content_sha256
        self.dependency_content_sha256 = dependency_content_sha256
        self.identity_sha256 = identity_sha256 or digest_json(
            {
                "builder_schema": "3.0",
                "revision": revision,
                "package_version": package_version,
                "source_content_sha256": source_content_sha256,
                "dependency_content_sha256": dependency_content_sha256,
                "platform": platform,
            }
        )
        self.platform = platform

    def prepare(self, runtime_root: Path) -> Path:
        self._validate_attestation()
        runtime_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifacts_root = runtime_root / "image-artifacts"
        artifacts_root.mkdir(exist_ok=True, mode=0o700)
        artifact_root = artifacts_root / self.identity_sha256
        lock_path = runtime_root.parent / f".{runtime_root.name}-image-artifacts.lock"
        with FileLock(lock_path, 60):
            if artifact_root.is_symlink():
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The cached Image runtime artifact is not a safe directory.",
                )
            if artifact_root.exists():
                if not artifact_root.is_dir():
                    raise HarnessError(
                        "PROCESS_START_FAILED",
                        "The cached Image runtime artifact is not a safe directory.",
                    )
                self._verify_read_only_artifact(artifact_root)
                marker = read_json(artifact_root / _MARKER)
                if marker != self._marker():
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The existing Image runtime artifact has another identity.",
                    )
                self._verify_artifact_content(artifact_root)
                return artifact_root
            temporary = artifacts_root / f".{self.identity_sha256}-{uuid.uuid4().hex}"
            try:
                temporary.mkdir(mode=0o700)
                self._copy_tree(self.source_root, temporary)
                actual_source_sha256 = content_tree_sha256(temporary)
                self._require_content_digest(
                    "source", actual_source_sha256, self.source_content_sha256
                )
                dependencies = temporary / "_dependencies"
                dependencies.mkdir(mode=0o700)
                self._copy_tree(self.dependency_root, dependencies)
                actual_dependency_sha256 = dependency_tree_sha256(dependencies)
                self._require_content_digest(
                    "dependency", actual_dependency_sha256, self.dependency_content_sha256
                )
                (temporary / IMAGE_ENTRYPOINT).write_text(_SERVER_SOURCE, encoding="utf-8")
                (temporary / IMAGE_WEB_REQUIREMENTS).write_text(
                    _WEB_REQUIREMENTS_SOURCE, encoding="utf-8"
                )
                atomic_write_json(temporary / _MARKER, self._marker(), mode=0o444)
                self._make_read_only(temporary)
                os.replace(temporary, artifact_root)
                fsync_directory(artifacts_root)
            except BaseException:
                if temporary.exists():
                    self._make_removable(temporary)
                    shutil.rmtree(temporary)
                if not any(artifacts_root.iterdir()):
                    artifacts_root.rmdir()
                raise
            return artifact_root

    def _verify_artifact_content(self, artifact_root: Path) -> None:
        actual_source_sha256 = content_tree_sha256(
            artifact_root,
            ignored_root_names={
                "_dependencies",
                IMAGE_ENTRYPOINT,
                IMAGE_WEB_REQUIREMENTS,
                _MARKER,
            },
        )
        actual_dependency_sha256 = dependency_tree_sha256(
            artifact_root / "_dependencies"
        )
        self._require_content_digest(
            "source", actual_source_sha256, self.source_content_sha256
        )
        self._require_content_digest(
            "dependency", actual_dependency_sha256, self.dependency_content_sha256
        )
        try:
            server_source = (artifact_root / IMAGE_ENTRYPOINT).read_text(encoding="utf-8")
            requirements = (artifact_root / IMAGE_WEB_REQUIREMENTS).read_text(
                encoding="utf-8"
            )
        except OSError:
            server_source = requirements = ""
        if server_source != _SERVER_SOURCE or requirements != _WEB_REQUIREMENTS_SOURCE:
            raise HarnessError(
                "PROCESS_START_FAILED",
                "The cached Image runtime bootstrap content is invalid.",
            )

    @staticmethod
    def _verify_read_only_artifact(artifact_root: Path) -> None:
        writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        try:
            root_metadata = artifact_root.lstat()
            if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_mode & writable:
                raise OSError("unsafe artifact root")
            for current, directories, files in os.walk(artifact_root, followlinks=False):
                for name in (*directories, *files):
                    metadata = (Path(current) / name).lstat()
                    if (
                        stat.S_ISLNK(metadata.st_mode)
                        or not (
                            stat.S_ISDIR(metadata.st_mode)
                            or stat.S_ISREG(metadata.st_mode)
                        )
                        or metadata.st_mode & writable
                    ):
                        raise OSError("unsafe artifact member")
        except OSError:
            raise HarnessError(
                "PROCESS_START_FAILED",
                "The cached Image runtime artifact is not read-only.",
            ) from None

    def _validate_attestation(self) -> None:
        for label, value in (
            ("source", self.source_content_sha256),
            ("dependency", self.dependency_content_sha256),
        ):
            if len(value) != _SHA256_LENGTH or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    f"The pinned Image Agent {label} digest is invalid.",
                )

    @staticmethod
    def _require_content_digest(label: str, actual: str, expected: str) -> None:
        if actual != expected:
            raise HarnessError(
                "PROCESS_START_FAILED",
                f"The Image Agent {label} content does not match its pinned revision.",
                {"expected_sha256": expected, "actual_sha256": actual},
            )

    def _copy_tree(
        self, source: Path, destination: Path, relative: Path = Path()
    ) -> None:
        current = source / relative
        for entry in sorted(os.scandir(current), key=lambda item: item.name):
            if self._ignored(entry.name, relative):
                continue
            source_path = Path(entry.path)
            target_path = destination / relative / entry.name
            if entry.is_symlink():
                raise HarnessError(
                    "PROCESS_START_FAILED", "The Image Agent source contains a symbolic link."
                )
            if entry.is_dir(follow_symlinks=False):
                target_path.mkdir(mode=0o700)
                self._copy_tree(source, destination, relative / entry.name)
                continue
            source_stat = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(source_stat.st_mode):
                raise HarnessError(
                    "PROCESS_START_FAILED", "The Image Agent source contains a special file."
                )
            shutil.copyfile(source_path, target_path, follow_symlinks=False)
            os.chmod(target_path, 0o444)

    @staticmethod
    def _ignored(name: str, relative: Path) -> bool:
        if name in _IGNORED_NAMES or name.endswith(_IGNORED_SUFFIXES):
            return True
        return relative == Path("frontend") and name == "data"

    @staticmethod
    def _make_read_only(root: Path) -> None:
        for current, directories, files in os.walk(root, topdown=False):
            for filename in files:
                os.chmod(Path(current) / filename, 0o444)
            for dirname in directories:
                os.chmod(Path(current) / dirname, 0o555)
        os.chmod(root, 0o555)

    @staticmethod
    def _make_removable(root: Path) -> None:
        for current, directories, files in os.walk(root, topdown=False):
            for filename in files:
                os.chmod(Path(current) / filename, 0o600)
            for dirname in directories:
                os.chmod(Path(current) / dirname, 0o700)
        os.chmod(root, 0o700)

    def _marker(self) -> dict[str, str]:
        return {
            "schema_version": "3.0",
            "identity_sha256": self.identity_sha256,
            "platform": self.platform,
            "source_revision": self.revision,
            "package_version": self.package_version,
            "source_content_sha256": self.source_content_sha256,
            "dependency_content_sha256": self.dependency_content_sha256,
            "entrypoint": IMAGE_ENTRYPOINT,
        }


def content_tree_sha256(
    root: Path,
    *,
    ignored_names: Collection[str] = (),
    ignored_root_names: Collection[str] = (),
    normalize_text_eol: bool = True,
) -> str:
    """Hash every runtime-relevant regular file by relative path and content."""

    manifest: list[dict[str, str | int]] = []
    _append_content_manifest(
        root,
        Path(),
        manifest,
        frozenset(ignored_names),
        frozenset(ignored_root_names),
        normalize_text_eol,
    )
    return digest_json(manifest)


def dependency_tree_sha256(root: Path) -> str:
    """Hash importable dependency content, excluding install-location metadata."""

    return content_tree_sha256(
        root,
        ignored_names=_DEPENDENCY_IGNORED_NAMES,
        ignored_root_names=_DEPENDENCY_IGNORED_ROOT_NAMES,
        normalize_text_eol=False,
    )


def _portable_file_bytes(path: Path) -> bytes:
    """Normalize only Git's cross-platform EOL transform for UTF-8 text."""

    content = path.read_bytes()
    if b"\0" in content:
        return content
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return content.replace(b"\r\n", b"\n")


def _append_content_manifest(
    root: Path,
    relative: Path,
    manifest: list[dict[str, str | int]],
    ignored_names: frozenset[str],
    ignored_root_names: frozenset[str],
    normalize_text_eol: bool,
) -> None:
    current = root / relative
    try:
        entries = sorted(os.scandir(current), key=lambda item: item.name)
    except OSError:
        raise HarnessError(
            "PROCESS_START_FAILED", "The Image Agent content tree cannot be inspected."
        ) from None
    for entry in entries:
        ignored_at_root = not relative.parts and entry.name in ignored_root_names
        if (
            entry.name in ignored_names
            or ignored_at_root
            or ImageRuntimeBuilder._ignored(entry.name, relative)
        ):
            continue
        path = Path(entry.path)
        item_relative = relative / entry.name
        if entry.is_symlink():
            raise HarnessError(
                "PROCESS_START_FAILED", "The Image Agent source contains a symbolic link."
            )
        if entry.is_dir(follow_symlinks=False):
            _append_content_manifest(
                root,
                item_relative,
                manifest,
                ignored_names,
                ignored_root_names,
                normalize_text_eol,
            )
            continue
        try:
            item_stat = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(item_stat.st_mode):
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The Image Agent source contains a special file.",
                )
            content = (
                _portable_file_bytes(path) if normalize_text_eol else path.read_bytes()
            )
        except OSError:
            raise HarnessError(
                "PROCESS_START_FAILED",
                "The Image Agent content tree cannot be inspected.",
            ) from None
        manifest.append(
            {
                "path": item_relative.as_posix(),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
