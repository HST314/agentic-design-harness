"""Immutable runtime artifact construction for the Image Agent adapter."""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from pathlib import Path

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, fsync_directory, read_json

IMAGE_ENTRYPOINT = "_harness_image_server.py"
IMAGE_WEB_REQUIREMENTS = "_harness-image-web.in"

_MARKER = ".harness-runtime-artifact.json"
_IGNORED_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
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
    ) -> None:
        self.source_root = source_root
        self.dependency_root = dependency_root
        self.revision = revision
        self.package_version = package_version

    def prepare(self, runtime_root: Path) -> Path:
        artifact_root = runtime_root / "image-agent-artifact"
        if artifact_root.exists():
            marker = read_json(artifact_root / _MARKER)
            if marker != self._marker():
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The existing Image runtime artifact has another revision.",
                )
            return artifact_root
        temporary = runtime_root / f".image-agent-artifact-{uuid.uuid4().hex}"
        try:
            temporary.mkdir(mode=0o700)
            self._copy_tree(self.source_root, temporary)
            dependencies = temporary / "_dependencies"
            dependencies.mkdir(mode=0o700)
            self._copy_tree(self.dependency_root, dependencies)
            (temporary / IMAGE_ENTRYPOINT).write_text(_SERVER_SOURCE, encoding="utf-8")
            (temporary / IMAGE_WEB_REQUIREMENTS).write_text(
                _WEB_REQUIREMENTS_SOURCE, encoding="utf-8"
            )
            atomic_write_json(temporary / _MARKER, self._marker(), mode=0o444)
            self._make_read_only(temporary)
            os.replace(temporary, artifact_root)
            fsync_directory(runtime_root)
        except BaseException:
            if temporary.exists():
                self._make_removable(temporary)
                shutil.rmtree(temporary)
            raise
        return artifact_root

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
            "schema_version": "1.0",
            "source_revision": self.revision,
            "package_version": self.package_version,
            "entrypoint": IMAGE_ENTRYPOINT,
        }
