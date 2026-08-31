"""Atomic, recoverable runtime artifact construction for the PPT Agent."""

from __future__ import annotations

import os
import re
import shutil
import stat
import uuid
from pathlib import Path

from ..core.errors import HarnessError
from ..services.process_runtime import (
    AgentRuntimeArtifact,
    AgentRuntimeIdentity,
    ProcessSpec,
    runtime_artifact_identity,
)
from ..storage.atomic import atomic_write_json, digest_json, fsync_directory, read_json
from ..storage.locks import FileLock
from .image_runtime import content_tree_sha256, dependency_tree_sha256

PPT_LAUNCHER = (
    "import runpy,sys; p=sys.argv[1]; "
    "sys.path.insert(0,str(__import__('pathlib').Path(p).parent)); "
    "m=runpy.run_path(p); "
    "__import__('uvicorn').run(m['app'],host=sys.argv[2],port=int(sys.argv[3]))"
)

_MARKER = ".harness-ppt-runtime.json"
_REQUIREMENTS_PROOF = "_harness-ppt-requirements.lock"
_IGNORED_NAMES = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache"})
_IGNORED_SUFFIXES = (".pyc", ".pyo")
_TEMPORARY_ARTIFACT = re.compile(r"^\.[0-9a-f]{64}-[0-9a-f]{32}$")


class PptRuntimeBuilder:
    """Publish one verified PPT source/dependency pair as a read-only artifact."""

    def __init__(
        self,
        source_root: Path,
        dependency_root: Path,
        requirements_lock: Path,
        *,
        revision: str,
        package_version: str,
        source_content_sha256: str,
        dependency_content_sha256: str,
    ) -> None:
        self.source_root = source_root.resolve(strict=True)
        self.dependency_root = dependency_root.resolve(strict=True)
        self.requirements_lock = requirements_lock.resolve(strict=True)
        self.revision = revision
        self.package_version = package_version
        self.source_content_sha256 = source_content_sha256
        self.dependency_content_sha256 = dependency_content_sha256
        self.identity_sha256 = digest_json(
            {
                "builder_schema": "1.0",
                "revision": revision,
                "package_version": package_version,
                "source_content_sha256": source_content_sha256,
                "dependency_content_sha256": dependency_content_sha256,
            }
        )
        self.cache_hit = False

    def prepare(self, cache_root: Path, *, force: bool = False) -> Path:
        """Build or verify the artifact, recovering safe builder-owned cache state."""

        stage = "initialize_cache"
        try:
            cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            artifact_root = cache_root / self.identity_sha256
            lock_path = cache_root.parent / f".{cache_root.name}.lock"
            with FileLock(lock_path, 60):
                self.cache_hit = False
                stage = "clean_incomplete_artifacts"
                self._remove_incomplete_artifacts(cache_root)
                stage = "inspect_cached_artifact"
                if artifact_root.is_symlink() or (
                    artifact_root.exists() and not artifact_root.is_dir()
                ):
                    raise HarnessError(
                        "ADAPTER_UNAVAILABLE",
                        "The cached PPT runtime artifact is not a safe directory.",
                    )
                if artifact_root.is_dir():
                    if force or not self._cached_artifact_is_valid(artifact_root):
                        stage = "invalidate_cached_artifact"
                        self._remove_tree(artifact_root)
                    else:
                        self.cache_hit = True
                        return artifact_root.resolve()
                temporary = cache_root / f".{self.identity_sha256}-{uuid.uuid4().hex}"
                try:
                    stage = "copy_source"
                    temporary.mkdir(mode=0o700)
                    self._copy_tree(self.source_root, temporary)
                    self._require_digest(
                        "source",
                        content_tree_sha256(temporary),
                        self.source_content_sha256,
                    )
                    stage = "copy_dependencies"
                    dependencies = temporary / "_dependencies"
                    dependencies.mkdir(mode=0o700)
                    self._copy_tree(self.dependency_root, dependencies, ignore=False)
                    self._require_digest(
                        "dependency",
                        dependency_tree_sha256(dependencies),
                        self.dependency_content_sha256,
                    )
                    shutil.copyfile(
                        self.requirements_lock,
                        temporary / _REQUIREMENTS_PROOF,
                    )
                    atomic_write_json(temporary / _MARKER, self._marker(), mode=0o444)
                    stage = "make_artifact_read_only"
                    self._make_read_only(temporary)
                    stage = "publish_artifact"
                    self._publish_artifact(temporary, artifact_root, cache_root)
                    stage = "verify_published_artifact"
                    self._verify_cached_artifact(artifact_root)
                    return artifact_root.resolve()
                except BaseException:
                    if temporary.exists():
                        self._remove_tree(temporary)
                    raise
        except HarnessError:
            raise
        except OSError as exc:
            details: dict[str, int | str] = {"stage": stage}
            if exc.errno is not None:
                details["errno"] = exc.errno
            winerror = getattr(exc, "winerror", None)
            if isinstance(winerror, int):
                details["winerror"] = winerror
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The PPT Agent runtime artifact could not be prepared.",
                details,
            ) from None

    def _cached_artifact_is_valid(self, artifact_root: Path) -> bool:
        try:
            self._verify_cached_artifact(artifact_root)
        except (HarnessError, OSError, ValueError):
            return False
        return True

    def _verify_cached_artifact(self, artifact_root: Path) -> None:
        self._verify_read_only(artifact_root)
        try:
            marker = read_json(artifact_root / _MARKER)
        except (OSError, ValueError):
            marker = None
        if marker != self._marker():
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The cached PPT runtime artifact has another identity.",
            )
        self._require_digest(
            "source",
            content_tree_sha256(
                artifact_root,
                ignored_names={_MARKER, _REQUIREMENTS_PROOF},
                ignored_root_names={"_dependencies"},
            ),
            self.source_content_sha256,
        )
        self._require_digest(
            "dependency",
            dependency_tree_sha256(artifact_root / "_dependencies"),
            self.dependency_content_sha256,
        )
        try:
            proof_matches = (
                (artifact_root / _REQUIREMENTS_PROOF).read_bytes()
                == self.requirements_lock.read_bytes()
            )
        except OSError:
            proof_matches = False
        if not proof_matches:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The cached PPT runtime dependency proof is invalid.",
            )

    def _remove_incomplete_artifacts(self, cache_root: Path) -> None:
        for entry in os.scandir(cache_root):
            if _TEMPORARY_ARTIFACT.fullmatch(entry.name) is None:
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                self._remove_tree(path)
            else:
                path.unlink()

    def _copy_tree(
        self,
        source: Path,
        destination: Path,
        relative: Path = Path(),
        *,
        ignore: bool = True,
    ) -> None:
        current = source / relative
        for entry in sorted(os.scandir(current), key=lambda item: item.name):
            if ignore and (
                entry.name in _IGNORED_NAMES
                or entry.name.endswith(_IGNORED_SUFFIXES)
            ):
                continue
            source_path = Path(entry.path)
            target_path = destination / relative / entry.name
            if entry.is_symlink():
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE",
                    "The PPT Agent runtime source contains a symbolic link.",
                )
            if entry.is_dir(follow_symlinks=False):
                target_path.mkdir(mode=0o700)
                self._copy_tree(
                    source,
                    destination,
                    relative / entry.name,
                    ignore=ignore,
                )
                continue
            metadata = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE",
                    "The PPT Agent runtime source contains a special file.",
                )
            shutil.copyfile(source_path, target_path, follow_symlinks=False)

    @staticmethod
    def _verify_read_only(root: Path) -> None:
        writable = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & writable:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "The cached PPT runtime artifact is not read-only.",
            )
        for current, directories, files in os.walk(root, followlinks=False):
            for name in (*directories, *files):
                member = (Path(current) / name).lstat()
                if (
                    stat.S_ISLNK(member.st_mode)
                    or not (stat.S_ISDIR(member.st_mode) or stat.S_ISREG(member.st_mode))
                    or member.st_mode & writable
                ):
                    raise HarnessError(
                        "ADAPTER_UNAVAILABLE",
                        "The cached PPT runtime artifact is not read-only.",
                    )

    @staticmethod
    def _make_read_only(root: Path) -> None:
        for current, directories, files in os.walk(root, topdown=False):
            for filename in files:
                os.chmod(Path(current) / filename, 0o444)
            for dirname in directories:
                os.chmod(Path(current) / dirname, 0o555)
        os.chmod(root, 0o555)

    @staticmethod
    def _publish_artifact(temporary: Path, artifact_root: Path, cache_root: Path) -> None:
        os.replace(temporary, artifact_root)
        fsync_directory(cache_root)

    @classmethod
    def _remove_tree(cls, root: Path) -> None:
        for current, directories, files in os.walk(root, topdown=False):
            for filename in files:
                os.chmod(Path(current) / filename, 0o600)
            for dirname in directories:
                os.chmod(Path(current) / dirname, 0o700)
        os.chmod(root, 0o700)
        shutil.rmtree(root)

    @staticmethod
    def _require_digest(label: str, actual: str, expected: str) -> None:
        if actual != expected:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                f"The PPT Agent {label} content does not match its release lock.",
            )

    def _marker(self) -> dict[str, str]:
        return {
            "schema_version": "1.0",
            "identity_sha256": self.identity_sha256,
            "source_revision": self.revision,
            "package_version": self.package_version,
            "source_content_sha256": self.source_content_sha256,
            "dependency_content_sha256": self.dependency_content_sha256,
            "entrypoint": "main_front.py",
        }


def verify_ppt_runtime_identity(
    artifact_root: Path,
    *,
    revision: str,
    interpreter: Path,
) -> AgentRuntimeIdentity:
    """Verify the process-runtime boundary against the published PPT artifact."""

    artifact = AgentRuntimeArtifact(
        "ppt-agent",
        revision,
        artifact_root,
        "main_front.py",
        ("pyproject.toml", _REQUIREMENTS_PROOF),
        interpreter.parent.parent,
    )
    return runtime_artifact_identity(
        ProcessSpec(
            command=(
                str(interpreter),
                "-c",
                PPT_LAUNCHER,
                str(artifact_root / "main_front.py"),
                "{host}",
                "{port}",
            ),
            runtime_artifact=artifact,
        )
    )
