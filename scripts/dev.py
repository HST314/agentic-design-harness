#!/usr/bin/env python3
"""Cross-platform bootstrap, diagnostics, and local process launcher."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

if TYPE_CHECKING:
    from harness.runtime_identity import (
        PythonInterpreterIdentity,
        RuntimeIdentityError,
        dependency_tree_sha256,
        inspect_python_interpreter,
    )
else:
    identity_path = BACKEND_ROOT / "harness" / "runtime_identity.py"
    identity_spec = importlib.util.spec_from_file_location(
        "_harness_stdlib_runtime_identity", identity_path
    )
    if identity_spec is None or identity_spec.loader is None:
        raise RuntimeError("Cannot load the dependency-free runtime identity primitives.")
    identity_module = importlib.util.module_from_spec(identity_spec)
    sys.modules[identity_spec.name] = identity_module
    identity_spec.loader.exec_module(identity_module)
    PythonInterpreterIdentity = identity_module.PythonInterpreterIdentity
    RuntimeIdentityError = identity_module.RuntimeIdentityError
    dependency_tree_sha256 = identity_module.dependency_tree_sha256
    inspect_python_interpreter = identity_module.inspect_python_interpreter

VENV_ROOT = ROOT / ".venv"
IMAGE_STAMP_NAME = ".requirements-installed"
PPT_DEPENDENCY_STAMP = ".requirements-installed.json"
FRONTEND_STAMP_NAME = ".harness-package-lock.sha256"
DEFAULT_BACKEND_PORT = 18080
DEFAULT_FRONTEND_PORT = 18180
DEFAULT_STARTUP_TIMEOUT_SECONDS = 120.0

_VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class LauncherError(RuntimeError):
    """An actionable local environment or child-process failure."""


class ConfigCheckFailed(LauncherError):
    """A fully rendered deployment configuration failure."""


@dataclass(frozen=True, slots=True)
class StartupConfiguration:
    revision: str
    backend_port: int


def _fail(message: str) -> NoReturn:
    raise LauncherError(message)


def _console_safe(value: str, *, encoding: str | None = None) -> str:
    """Make child output printable even on a legacy Windows console encoding."""

    selected_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        value.encode(selected_encoding)
    except UnicodeEncodeError:
        return value.encode(selected_encoding, errors="backslashreplace").decode(selected_encoding)
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def input_digest(paths: Sequence[Path], *, identity: Sequence[str] = (), root: Path = ROOT) -> str:
    """Hash named inputs without depending on absolute checkout locations."""

    digest = hashlib.sha256()
    for value in identity:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for path in sorted(paths, key=lambda item: item.as_posix()):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = path.name
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def content_tree_digest(root: Path) -> str:
    """Hash installed dependency content while ignoring location-specific metadata."""

    try:
        return dependency_tree_sha256(root)
    except RuntimeIdentityError as exc:
        _fail(str(exc))


def venv_python(venv_root: Path = VENV_ROOT, *, os_name: str | None = None) -> Path:
    selected_os = os.name if os_name is None else os_name
    return (
        venv_root / "Scripts" / "python.exe"
        if selected_os == "nt"
        else venv_root / "bin" / "python"
    )


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail(f"Cannot read required JSON file {path}: {exc}")
    if not isinstance(value, dict):
        _fail(f"Required JSON file is not an object: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def executable(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        _fail(f"Required command '{name}' was not found on PATH.")
    return found


def _version_tuple(value: str) -> tuple[int, int, int]:
    matched = _VERSION.search(value)
    if matched is None:
        _fail(f"Could not parse tool version from: {value.strip()}")
    return tuple(int(part or 0) for part in matched.groups())


def run_command(
    command: Sequence[str | Path],
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    normalized = [str(item) for item in command]
    print("$ " + subprocess.list2cmdline(normalized), flush=True)
    return subprocess.run(
        normalized,
        cwd=cwd,
        env=environment,
        check=True,
        text=True,
        capture_output=capture,
    )


def command_succeeds(
    command: Sequence[str | Path],
    *,
    cwd: Path = ROOT,
    environment: dict[str, str] | None = None,
) -> bool:
    try:
        subprocess.run(
            [str(item) for item in command],
            cwd=cwd,
            env=environment,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _pythonpath(environment: dict[str, str], *paths: Path) -> dict[str, str]:
    updated = environment.copy()
    entries = [str(path) for path in paths]
    existing = updated.get("PYTHONPATH")
    if existing:
        entries.append(existing)
    updated["PYTHONPATH"] = os.pathsep.join(entries)
    return updated


@dataclass(slots=True)
class DevelopmentLauncher:
    root: Path = ROOT
    python: Path = Path(sys.executable)

    @property
    def lock_path(self) -> Path:
        return self.root / "agents" / "image-agent.lock.json"

    @property
    def image_agent_root(self) -> Path:
        lock = load_json(self.lock_path)
        relative = Path(str(lock.get("embedded_path", "")))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            _fail("Image Agent lock contains an unsafe embedded path.")
        candidate = self.root / relative
        if candidate.resolve(strict=False).parent != (self.root / "agents").resolve():
            _fail("Image Agent lock must point to one direct child of agents/.")
        return candidate

    @property
    def ppt_agent_root(self) -> Path:
        return self.root / "agents" / "ppt-agent"

    @property
    def ppt_agent_lock_path(self) -> Path:
        return self.root / "agents" / "ppt-agent.lock.json"

    @property
    def ppt_requirements_lock(self) -> Path:
        return self.root / "requirements" / "ppt-agent.lock"

    @property
    def venv_root(self) -> Path:
        return self.root / ".venv"

    @property
    def venv_python(self) -> Path:
        return venv_python(self.venv_root)

    @property
    def runtime_root(self) -> Path:
        return self.root / ".runtime"

    @property
    def image_dependency_root(self) -> Path:
        return self.runtime_root / "image-agent-deps"

    @property
    def ppt_dependency_root(self) -> Path:
        return self.runtime_root / "ppt-agent-deps"

    @property
    def image_runtime_root(self) -> Path:
        return self.runtime_root / "image-runtime"

    @property
    def image_python(self) -> Path:
        return self.venv_python

    @property
    def frontend_root(self) -> Path:
        return self.root / "frontend"

    @staticmethod
    def interpreter_identity(
        command: Sequence[str | Path],
    ) -> PythonInterpreterIdentity:
        try:
            return inspect_python_interpreter(command)
        except RuntimeIdentityError as exc:
            _fail(str(exc))

    @staticmethod
    def _same_interpreter_runtime(
        first: PythonInterpreterIdentity, second: PythonInterpreterIdentity
    ) -> bool:
        return (
            first.implementation,
            first.cache_tag,
            first.version,
        ) == (
            second.implementation,
            second.cache_tag,
            second.version,
        )

    @staticmethod
    def _identity_matches_path(identity: PythonInterpreterIdentity, path: Path) -> bool:
        return os.path.normcase(os.path.abspath(identity.executable)) == os.path.normcase(
            os.path.abspath(path)
        )

    def pip_environment(self) -> dict[str, str]:
        cache = self.runtime_root / "pip-cache"
        cache.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["PIP_CACHE_DIR"] = str(cache)
        return environment

    def npm_environment(self) -> dict[str, str]:
        cache = self.runtime_root / "npm-cache"
        cache.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["NPM_CONFIG_CACHE"] = str(cache)
        environment["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
        return environment

    def check_tools(self) -> None:
        git = executable("git")
        node = executable("node")
        npm = executable("npm")
        git_version = run_command([git, "--version"], capture=True).stdout.strip()
        node_version = run_command([node, "--version"], capture=True).stdout.strip()
        npm_version = run_command([npm, "--version"], capture=True).stdout.strip()
        if _version_tuple(node_version) < (22, 0, 0):
            _fail(f"Node.js 22 or newer is required; found {node_version}.")
        print(
            f"[ok] Python {platform.python_version()} ({self.python}), "
            f"{git_version} ({git}), Node {node_version} ({node}), npm {npm_version}",
            flush=True,
        )

    def ensure_submodule(self) -> None:
        git = executable("git")
        relatives = (
            self.image_agent_root.relative_to(self.root).as_posix(),
            self.ppt_agent_root.relative_to(self.root).as_posix(),
        )
        for relative in relatives:
            run_command([git, "submodule", "sync", "--", relative], cwd=self.root)
            run_command(
                [
                    git,
                    "submodule",
                    "update",
                    "--init",
                    "--checkout",
                    "--recursive",
                    "--",
                    relative,
                ],
                cwd=self.root,
            )
        self.verify_image_lock()
        self.verify_ppt_lock()

    def verify_image_lock(self) -> None:
        run_command(
            [
                self.python,
                self.root / "scripts" / "verify_image_agent_lock.py",
                "--image-agent-root",
                self.image_agent_root,
            ],
            cwd=self.root,
        )

    def verify_ppt_lock(self) -> None:
        run_command(
            [self.python, self.root / "scripts" / "verify_ppt_agent_lock.py"],
            cwd=self.root,
        )

    def backend_input_digest(self, identity: PythonInterpreterIdentity | None = None) -> str:
        selected = identity or self.interpreter_identity([self.venv_python])
        return input_digest(
            [self.root / "requirements-runtime.txt", self.root / "pyproject.toml"],
            identity=(
                f"python_implementation={selected.implementation}",
                f"python_cache_tag={selected.cache_tag}",
                f"python_version={selected.version}",
                f"platform={sys.platform}",
                f"machine={platform.machine().lower()}",
            ),
            root=self.root,
        )

    def backend_environment(self) -> dict[str, str]:
        return _pythonpath(os.environ.copy(), self.root / "backend")

    def backend_is_current(self) -> bool:
        if not self.venv_python.is_file():
            return False
        stamp = self.venv_root / ".harness-runtime.json"
        try:
            current = load_json(stamp)
            identity = self.interpreter_identity([self.venv_python])
        except LauncherError:
            return False
        if (
            not identity.is_virtual_environment
            or not self._identity_matches_path(identity, self.venv_python)
            or current.get("input_sha256") != self.backend_input_digest(identity)
            or current.get("interpreter") != identity.as_dict()
        ):
            return False
        return command_succeeds(
            [
                self.venv_python,
                "-c",
                "import fastapi, jsonschema, PIL, pydantic, uvicorn, yaml; import harness",
            ],
            cwd=self.root,
            environment=self.backend_environment(),
        )

    def ensure_virtual_environment(self) -> None:
        expected = self.interpreter_identity([self.python])
        if self.venv_python.is_file():
            try:
                actual = self.interpreter_identity([self.venv_python])
            except LauncherError:
                actual = None
            if (
                actual is not None
                and actual.is_virtual_environment
                and self._identity_matches_path(actual, self.venv_python)
                and self._same_interpreter_runtime(actual, expected)
            ):
                return
            print(
                "Existing Harness virtual environment has another interpreter identity; "
                "rebuilding it.",
                flush=True,
            )
        backup: Path | None = None
        failed: Path | None = None
        if self.venv_root.exists():
            backup = self.root / f".venv-backup-{uuid.uuid4().hex}"
            self.venv_root.replace(backup)
        try:
            try:
                run_command([self.python, "-m", "venv", self.venv_root], cwd=self.root)
            except subprocess.CalledProcessError:
                if self.venv_root.exists():
                    failed = self.root / f".venv-failed-{uuid.uuid4().hex}"
                    self.venv_root.replace(failed)
                print(
                    "Standard venv bootstrap has no ensurepip; retrying with the "
                    "interpreter's existing pip.",
                    flush=True,
                )
                run_command(
                    [self.python, "-m", "venv", "--without-pip", self.venv_root],
                    cwd=self.root,
                )
            if not self.venv_python.is_file():
                _fail("Python could not create a usable virtual environment.")
            actual = self.interpreter_identity([self.venv_python])
            if (
                not actual.is_virtual_environment
                or not self._identity_matches_path(actual, self.venv_python)
                or not self._same_interpreter_runtime(actual, expected)
            ):
                _fail("Python created a virtual environment with another interpreter identity.")
        except BaseException:
            if self.venv_root.exists():
                replacement = self.root / f".venv-invalid-{uuid.uuid4().hex}"
                self.venv_root.replace(replacement)
                shutil.rmtree(replacement)
            if failed is not None and failed.exists():
                shutil.rmtree(failed)
            if backup is not None and backup.exists():
                backup.replace(self.venv_root)
            raise
        if backup is not None:
            shutil.rmtree(backup)
        if failed is not None:
            shutil.rmtree(failed)

    def pip_installer(
        self,
    ) -> tuple[list[str], Path | None, PythonInterpreterIdentity]:
        if command_succeeds([self.venv_python, "-m", "pip", "--version"]):
            return (
                [str(self.venv_python), "-m", "pip"],
                None,
                self.interpreter_identity([self.venv_python]),
            )
        if not command_succeeds([self.python, "-m", "pip", "--version"]):
            _fail("pip is unavailable. Install the Python pip/venv components and rerun setup.")
        installer_identity = self.interpreter_identity([self.python])
        if os.name == "nt":
            site_packages = self.venv_root / "Lib" / "site-packages"
        else:
            major, minor, *_ = installer_identity.version.split(".")
            site_packages = self.venv_root / "lib" / f"python{major}.{minor}" / "site-packages"
        site_packages.mkdir(parents=True, exist_ok=True)
        return [str(self.python), "-m", "pip"], site_packages, installer_identity

    def install_backend(self, *, force: bool = False) -> None:
        self.ensure_virtual_environment()
        if not force and self.backend_is_current():
            print("[ok] Harness Python environment matches its lock.", flush=True)
            return
        installer, external_target, installer_identity = self.pip_installer()
        target_arguments: list[str | Path] = (
            [] if external_target is None else ["--target", external_target]
        )
        run_command(
            [
                *installer,
                "install",
                "--disable-pip-version-check",
                "--require-hashes",
                "--upgrade",
                *target_arguments,
                "-r",
                self.root / "requirements-runtime.txt",
            ],
            cwd=self.root,
            environment=self.pip_environment(),
        )
        if not self.backend_is_importable():
            _fail("Harness dependencies were installed but required imports still fail.")
        runtime_identity = self.interpreter_identity([self.venv_python])
        write_json(
            self.venv_root / ".harness-runtime.json",
            {
                "schema_version": "2.0",
                "input_sha256": self.backend_input_digest(runtime_identity),
                "interpreter": runtime_identity.as_dict(),
                "installer": installer_identity.as_dict(),
                "platform": sys.platform,
            },
        )

    def backend_is_importable(self) -> bool:
        return command_succeeds(
            [
                self.venv_python,
                "-c",
                "import fastapi, jsonschema, PIL, pydantic, uvicorn, yaml; import harness",
            ],
            cwd=self.root,
            environment=self.backend_environment(),
        )

    def image_input_digest(
        self,
        runtime_identity: PythonInterpreterIdentity | None = None,
        installer_identity: PythonInterpreterIdentity | None = None,
    ) -> str:
        runtime = runtime_identity or self.interpreter_identity([self.image_python])
        if installer_identity is None:
            _, _, installer = self.pip_installer()
        else:
            installer = installer_identity
        lock = load_json(self.lock_path)
        paths: list[Path] = [self.lock_path]
        dependencies = lock.get("dependencies")
        if not isinstance(dependencies, dict) or not isinstance(dependencies.get("files"), list):
            _fail("Image Agent lock has no dependency file list.")
        for item in dependencies["files"]:
            if not isinstance(item, dict):
                _fail("Image Agent lock contains an invalid dependency record.")
            scope = item.get("scope")
            relative = Path(str(item.get("path", "")))
            paths.append((self.root if scope == "harness" else self.image_agent_root) / relative)
        return input_digest(
            paths,
            identity=(
                f"runtime_implementation={runtime.implementation}",
                f"runtime_cache_tag={runtime.cache_tag}",
                f"runtime_version={runtime.version}",
                f"installer_implementation={installer.implementation}",
                f"installer_cache_tag={installer.cache_tag}",
                f"installer_version={installer.version}",
                f"platform={sys.platform}",
                f"machine={platform.machine().lower()}",
            ),
            root=self.root,
        )

    def require_current_image_dependencies(
        self,
    ) -> tuple[PythonInterpreterIdentity, PythonInterpreterIdentity]:
        root = self.image_dependency_root
        stamp_path = root / IMAGE_STAMP_NAME
        if not root.is_dir() or not stamp_path.is_file():
            _fail(
                "Image Agent dependencies are not installed; run " "scripts/dev.py setup --force."
            )
        stamp = load_json(stamp_path)
        runtime_identity = self.interpreter_identity([self.image_python])
        _, _, installer_identity = self.pip_installer()
        actual_digest = content_tree_digest(root)
        if not runtime_identity.is_virtual_environment or not self._identity_matches_path(
            runtime_identity, self.image_python
        ):
            _fail(
                "Image Agent would run with an unexpected Python interpreter; rebuild "
                "the Harness environment with scripts/dev.py setup --force."
            )
        expected_input = self.image_input_digest(runtime_identity, installer_identity)
        if (
            stamp.get("schema_version") != "2.0"
            or stamp.get("input_sha256") != expected_input
            or stamp.get("interpreter") != runtime_identity.as_dict()
            or stamp.get("installer") != installer_identity.as_dict()
        ):
            _fail(
                "Image Agent dependencies were installed by another interpreter or lock "
                "set; run scripts/dev.py setup --force."
            )
        if stamp.get("content_sha256") != actual_digest:
            _fail(
                "Image Agent dependencies changed after setup; run " "scripts/dev.py setup --force."
            )
        return runtime_identity, installer_identity

    def image_dependencies_are_current(self) -> bool:
        try:
            self.require_current_image_dependencies()
        except LauncherError:
            return False
        return True

    def attest_image_runtime(
        self,
        dependency_root: Path | None = None,
        *,
        prepare_artifact: bool = False,
    ) -> dict[str, Any]:
        selected_dependencies = dependency_root or self.image_dependency_root
        command = [
            self.venv_python,
            self.root / "scripts" / "attest_image_runtime.py",
            "--lock",
            self.lock_path,
            "--source",
            self.image_agent_root,
            "--dependencies",
            selected_dependencies,
            "--harness-root",
            self.root,
            "--interpreter",
            self.image_python,
        ]
        if prepare_artifact:
            command.extend(("--cache-root", self.image_runtime_root))
        completed = subprocess.run(
            [str(item) for item in command],
            cwd=self.root,
            env=self.backend_environment(),
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            rendered = completed.stderr.strip() or completed.stdout.strip()
            try:
                failure = json.loads(rendered)
            except json.JSONDecodeError:
                failure = None
            if isinstance(failure, dict):
                message = failure.get("message")
                details = failure.get("details")
                action = details.get("action") if isinstance(details, dict) else None
                if isinstance(message, str) and message:
                    suffix = f" {action}" if isinstance(action, str) and action else ""
                    _fail(f"Image Agent environment is unusable: {message}{suffix}")
            _fail(f"Image Agent environment validation failed: {rendered}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            _fail("Image Agent runtime attestation returned invalid output.")
        if not isinstance(result, dict):
            _fail("Image Agent runtime attestation returned invalid output.")
        return result

    def prepare_image_runtime(self) -> dict[str, Any]:
        """Attest and prewarm the immutable artifact before health timing starts."""

        print(
            "[prepare] Verifying and warming the Image Agent runtime artifact...",
            flush=True,
        )
        started = time.monotonic()
        attestation = self.attest_image_runtime(prepare_artifact=True)
        artifact_root = attestation.get("artifact_root")
        cache_hit = attestation.get("artifact_cache_hit")
        if not isinstance(artifact_root, str) or not isinstance(cache_hit, bool):
            _fail("Image Agent runtime preparation returned invalid output.")
        elapsed = time.monotonic() - started
        disposition = "reused" if cache_hit else "prepared"
        print(
            f"[ok] Image Agent runtime artifact {disposition} in {elapsed:.1f}s",
            flush=True,
        )
        return attestation

    def install_image_dependencies(self, *, force: bool = False) -> None:
        if not force and self.image_dependencies_are_current():
            print("[ok] Image Agent isolated dependencies match their locks.", flush=True)
            return
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".image-agent-deps-", dir=self.runtime_root))
        backup: Path | None = None
        try:
            installer, _, installer_identity = self.pip_installer()
            run_command(
                [
                    *installer,
                    "install",
                    "--disable-pip-version-check",
                    "--upgrade",
                    "--target",
                    temporary,
                    "-r",
                    self.image_agent_root / "requirements.lock",
                    "-r",
                    self.root / "requirements" / "image-agent-web.in",
                ],
                cwd=self.root,
                environment=self.pip_environment(),
            )
            attestation = self.attest_image_runtime(temporary)
            content_sha256 = attestation.get("dependency_sha256")
            if not isinstance(content_sha256, str):
                _fail("Image Agent environment validation returned no dependency identity.")
            runtime_identity = self.interpreter_identity([self.image_python])
            write_json(
                temporary / IMAGE_STAMP_NAME,
                {
                    "schema_version": "2.0",
                    "input_sha256": self.image_input_digest(runtime_identity, installer_identity),
                    "content_sha256": content_sha256,
                    "interpreter": runtime_identity.as_dict(),
                    "installer": installer_identity.as_dict(),
                    "platform": sys.platform,
                },
            )
            if self.image_dependency_root.exists():
                backup = self.runtime_root / f".image-agent-deps-backup-{uuid.uuid4().hex}"
                self.image_dependency_root.replace(backup)
            temporary.replace(self.image_dependency_root)
            if backup is not None:
                shutil.rmtree(backup)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            if backup is not None and backup.exists() and not self.image_dependency_root.exists():
                backup.replace(self.image_dependency_root)
            raise

    def install_ppt_dependencies(
        self,
        *,
        force: bool = False,
        runtime_python: Path | None = None,
    ) -> None:
        """Install PPT Agent's declared runtime dependencies in an isolated target."""

        selected_python = Path(os.path.abspath(runtime_python or self.venv_python))
        if not selected_python.is_file():
            _fail("The selected PPT Agent runtime interpreter is unavailable.")
        release_lock = load_json(self.ppt_agent_lock_path)
        dependencies = release_lock.get("dependencies")
        if not isinstance(dependencies, dict) or not isinstance(
            dependencies.get("lock_set_sha256"), str
        ):
            _fail("PPT Agent release lock has no deterministic dependency set.")
        if not force and self.ppt_runtime_is_attested(
            self.ppt_dependency_root,
            interpreter=selected_python,
        ):
            print("[ok] PPT Agent isolated dependencies match their lock.", flush=True)
            return
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".ppt-agent-deps-", dir=self.runtime_root))
        backup: Path | None = None
        try:
            if runtime_python is None:
                installer, _, _ = self.pip_installer()
            else:
                if not command_succeeds([selected_python, "-m", "pip", "--version"]):
                    _fail("pip is unavailable for the selected PPT Agent interpreter.")
                installer = [str(selected_python), "-m", "pip"]
            run_command(
                [
                    *installer,
                    "install",
                    "--disable-pip-version-check",
                    "--upgrade",
                    "--require-hashes",
                    "--target",
                    temporary,
                    "-r",
                    self.ppt_requirements_lock,
                ],
                cwd=self.root,
                environment=self.pip_environment(),
            )
            runtime_identity = self.interpreter_identity([selected_python])
            write_json(
                temporary / PPT_DEPENDENCY_STAMP,
                {
                    "schema_version": "1.0",
                    "dependency_lock_set_sha256": dependencies["lock_set_sha256"],
                    "dependency_sha256": content_tree_digest(temporary),
                    "interpreter": {
                        "implementation": runtime_identity.implementation,
                        "cache_tag": runtime_identity.cache_tag,
                        "version": runtime_identity.version,
                    },
                },
            )
            if not self.ppt_runtime_is_attested(
                temporary,
                interpreter=selected_python,
            ):
                _fail("PPT Agent dependencies were installed but failed runtime attestation.")
            if self.ppt_dependency_root.exists():
                backup = self.runtime_root / f".ppt-agent-deps-backup-{uuid.uuid4().hex}"
                self.ppt_dependency_root.replace(backup)
            temporary.replace(self.ppt_dependency_root)
            if backup is not None:
                shutil.rmtree(backup)
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            if backup is not None and backup.exists() and not self.ppt_dependency_root.exists():
                backup.replace(self.ppt_dependency_root)
            raise

    def ppt_runtime_is_attested(
        self,
        dependency_root: Path,
        *,
        interpreter: Path | None = None,
    ) -> bool:
        """Run the authoritative proof inside the installed Harness interpreter."""

        selected_python = Path(os.path.abspath(interpreter or self.venv_python))
        if not selected_python.is_file():
            return False
        code = (
            "from pathlib import Path; "
            "from harness.adapters.ppt_attestation import attest_ppt_runtime; "
            "from harness.adapters.ppt_lock import load_ppt_agent_lock; "
            "import sys; "
            "attest_ppt_runtime(load_ppt_agent_lock(Path(sys.argv[1])), "
            "source_root=Path(sys.argv[2]), dependency_root=Path(sys.argv[3]), "
            "harness_root=Path(sys.argv[4]), interpreter=Path(sys.argv[5]))"
        )
        return command_succeeds(
            [
                selected_python,
                "-c",
                code,
                self.ppt_agent_lock_path,
                self.ppt_agent_root,
                dependency_root,
                self.root,
                selected_python,
            ],
            cwd=self.root,
            environment=self.backend_environment(),
        )

    def frontend_input_digest(self) -> str:
        return input_digest([self.frontend_root / "package-lock.json"], root=self.root)

    def frontend_is_current(self) -> bool:
        node_modules = self.frontend_root / "node_modules"
        stamp = node_modules / FRONTEND_STAMP_NAME
        if not node_modules.is_dir() or not stamp.is_file():
            return False
        try:
            if stamp.read_text(encoding="utf-8").strip() != self.frontend_input_digest():
                return False
        except OSError:
            return False
        npm = shutil.which("npm")
        return npm is not None and command_succeeds(
            [npm, "--prefix", self.frontend_root, "ls", "--depth=0"],
            cwd=self.root,
            environment=self.npm_environment(),
        )

    def install_frontend(self, *, force: bool = False) -> None:
        if not force and self.frontend_is_current():
            print("[ok] Frontend node_modules matches package-lock.json.", flush=True)
            return
        npm = executable("npm")
        run_command(
            [npm, "ci"],
            cwd=self.frontend_root,
            environment=self.npm_environment(),
        )
        stamp = self.frontend_root / "node_modules" / FRONTEND_STAMP_NAME
        stamp.write_text(self.frontend_input_digest() + "\n", encoding="utf-8")
        if not self.frontend_is_current():
            _fail("Frontend dependencies are incomplete after npm ci.")

    def setup(self, *, force: bool = False) -> None:
        self.check_tools()
        self.ensure_submodule()
        self.install_backend(force=force)
        self.install_image_dependencies(force=force)
        self.install_ppt_dependencies(force=force)
        self.prepare_image_runtime()
        self.install_frontend(force=force)
        print("[ok] Development environment is ready.", flush=True)

    def config_check(self) -> StartupConfiguration:
        """Validate deployment configuration without creating files or calling providers."""

        if self.venv_python.is_file() and command_succeeds(
            [self.venv_python, "-c", "import pydantic, yaml"]
        ):
            code = (
                "import json, sys; from pathlib import Path; "
                "from harness.core.config_kernel import ConfigurationError, "
                "load_config_snapshot; "
                "root=Path(sys.argv[1]); "
                "\ntry: snapshot=load_config_snapshot(root)"
                "\nexcept ConfigurationError as exc: print(str(exc), file=sys.stderr); "
                "raise SystemExit(2)"
                "\nprint(json.dumps({'revision': snapshot.revision, "
                "'backend_port': snapshot.runtime.server.port}))"
            )
            result = subprocess.run(
                [str(self.venv_python), "-c", code, str(self.root)],
                cwd=self.root,
                env=self.backend_environment(),
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                error = result.stderr.strip()
                if error.startswith("CONFIG_ERROR:"):
                    raise ConfigCheckFailed(error)
                _fail("Configuration checker failed: " + (error or "unknown error"))
            try:
                checked = json.loads(result.stdout)
                startup = StartupConfiguration(
                    revision=str(checked["revision"]),
                    backend_port=int(checked["backend_port"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                _fail(f"Configuration checker returned an invalid result: {exc}")
        else:
            startup = self._config_check_in_process()
        print(
            f"[ok] Configuration snapshot {startup.revision} is valid (no provider calls).",
            flush=True,
        )
        return startup

    def _config_check_in_process(self) -> StartupConfiguration:
        backend_root = ROOT / "backend"
        inserted = str(backend_root) not in sys.path
        if inserted:
            sys.path.insert(0, str(backend_root))
        try:
            from harness.core.config_kernel import ConfigurationError, load_config_snapshot

            try:
                snapshot = load_config_snapshot(self.root)
            except ConfigurationError as exc:
                raise ConfigCheckFailed(str(exc)) from exc
        except ModuleNotFoundError as exc:
            _fail(
                "Configuration dependencies are unavailable; run scripts/dev.py setup first "
                f"({exc.name})."
            )
        finally:
            if inserted:
                sys.path.remove(str(backend_root))
        return StartupConfiguration(
            revision=snapshot.revision,
            backend_port=snapshot.runtime.server.port,
        )

    def _check_configuration(self) -> None:
        code = (
            "from pathlib import Path; from harness.core.config import load_settings; "
            f"s=load_settings(Path({str(self.root)!r})); "
            "assert s.contracts_root.is_dir(); assert s.image_agent_root.is_dir(); "
            "assert s.image_agent_lock_path.is_file(); "
            "assert s.image_agent_python.is_file()"
            "; assert s.ppt_agent_root.is_dir(); assert s.ppt_agent_lock_path.is_file(); "
            "assert s.ppt_agent_python.is_file(); assert s.ppt_agent_runtime_policy.is_file(); "
            "assert s.ppt_agent_model_config.is_file()"
        )
        if not command_succeeds(
            [self.venv_python, "-c", code],
            cwd=self.root,
            environment=self.backend_environment(),
        ):
            _fail("Harness configuration paths are incomplete or invalid.")

    def _check_writable_runtime(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor, raw_path = tempfile.mkstemp(prefix=".doctor-", dir=self.runtime_root)
            os.close(descriptor)
            Path(raw_path).unlink()
        except OSError as exc:
            _fail(f"Runtime directory is not writable: {exc}")

    @staticmethod
    def _check_port(host: str, port: int) -> None:
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name != "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError as exc:
                _fail(f"Port {host}:{port} is unavailable: {exc}")

    def doctor(
        self,
        *,
        check_ports: bool = True,
        backend_port: int = DEFAULT_BACKEND_PORT,
        frontend_port: int = DEFAULT_FRONTEND_PORT,
        configuration_checked: bool = False,
        allow_image_degraded: bool = False,
        allow_ppt_degraded: bool = False,
    ) -> None:
        if not configuration_checked:
            self.config_check()
        self.check_tools()
        self.verify_image_lock()
        self.verify_ppt_lock()
        if not self.backend_is_current():
            _fail("Harness Python environment is missing or stale; run scripts/dev.py setup.")
        print("[ok] Harness Python environment", flush=True)
        image_degraded = False
        try:
            runtime_identity, installer_identity = self.require_current_image_dependencies()
            attestation = self.prepare_image_runtime()
        except LauncherError as exc:
            if not allow_image_degraded:
                raise
            image_degraded = True
            print(
                "[degraded] Image Adapter will be disabled: " + str(exc),
                flush=True,
            )
        else:
            print(
                "[ok] Image Agent isolated dependency environment "
                f"(runtime {runtime_identity.implementation}/"
                f"{runtime_identity.cache_tag}; pip "
                f"{installer_identity.implementation}/{installer_identity.cache_tag}; "
                f"packages {attestation['package_name']} "
                f"{attestation['package_version']})",
                flush=True,
            )
        ppt_degraded = False
        if not self.ppt_runtime_is_attested(self.ppt_dependency_root):
            message = (
                "PPT Agent dependencies are missing, stale, or inconsistent; "
                "run scripts/dev.py setup-ppt-runtime --force."
            )
            if not allow_ppt_degraded:
                _fail(message)
            ppt_degraded = True
            print("[degraded] PPT Adapter will be disabled: " + message, flush=True)
        else:
            print("[ok] PPT Agent isolated dependencies match their lock.", flush=True)
        if not self.frontend_is_current():
            _fail("Frontend node_modules is missing or stale; run scripts/dev.py setup.")
        print("[ok] Frontend package-lock installation", flush=True)
        self._check_configuration()
        print("[ok] Harness configuration paths", flush=True)
        self._check_writable_runtime()
        print("[ok] Runtime directory permissions", flush=True)
        if check_ports:
            self._check_port("127.0.0.1", backend_port)
            self._check_port("127.0.0.1", frontend_port)
            print(
                f"[ok] Ports {backend_port} and {frontend_port} are available",
                flush=True,
            )
        print(
            "[degraded] Doctor completed; control-plane startup remains available."
            if image_degraded or ppt_degraded
            else "[ok] Doctor completed without errors.",
            flush=True,
        )

    def start(
        self,
        *,
        frontend_port: int = DEFAULT_FRONTEND_PORT,
        timeout_seconds: float = DEFAULT_STARTUP_TIMEOUT_SECONDS,
        check_only: bool = False,
    ) -> int:
        snapshot = self.config_check()
        backend_port = snapshot.backend_port
        self.doctor(
            check_ports=True,
            backend_port=backend_port,
            frontend_port=frontend_port,
            configuration_checked=True,
            allow_image_degraded=True,
            allow_ppt_degraded=True,
        )
        backend_environment = self.backend_environment()
        frontend_environment = self.npm_environment()
        frontend_environment["HARNESS_BACKEND_URL"] = f"http://127.0.0.1:{backend_port}"
        npm = executable("npm")
        specifications = (
            ChildSpecification(
                name="backend",
                command=(str(self.venv_python), "-m", "harness"),
                cwd=self.root,
                environment=backend_environment,
            ),
            ChildSpecification(
                name="frontend",
                command=(
                    npm,
                    "exec",
                    "--",
                    "vite",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(frontend_port),
                ),
                cwd=self.frontend_root,
                environment=frontend_environment,
            ),
        )
        group = ChildGroup(specifications)
        try:
            group.start()
            group.wait_until_healthy(
                (
                    f"http://127.0.0.1:{backend_port}/healthz",
                    f"http://127.0.0.1:{backend_port}/readyz",
                    f"http://127.0.0.1:{frontend_port}/",
                ),
                timeout_seconds=timeout_seconds,
            )
            print(
                f"[ready] Web http://127.0.0.1:{frontend_port}/ | "
                f"API http://127.0.0.1:{backend_port}/docs",
                flush=True,
            )
            if check_only:
                return 0
            return group.wait()
        except KeyboardInterrupt:
            print("\nStopping backend and frontend...", flush=True)
            return 130
        finally:
            group.stop()


@dataclass(frozen=True, slots=True)
class ChildSpecification:
    name: str
    command: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]


class ChildGroup:
    def __init__(self, specifications: Sequence[ChildSpecification]) -> None:
        self.specifications = tuple(specifications)
        self.processes: list[tuple[ChildSpecification, subprocess.Popen[str]]] = []
        self.threads: list[threading.Thread] = []

    def start(self) -> None:
        for specification in self.specifications:
            kwargs: dict[str, Any] = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(
                    list(specification.command),
                    cwd=specification.cwd,
                    env=specification.environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    **kwargs,
                )
            except OSError as exc:
                _fail(f"Could not start {specification.name}: {exc}")
            self.processes.append((specification, process))
            thread = threading.Thread(
                target=self._pump,
                args=(specification.name, process),
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)

    @staticmethod
    def _pump(name: str, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            print(_console_safe(f"[{name}] {line.rstrip()}"), flush=True)

    def _raise_if_exited(self) -> None:
        for specification, process in self.processes:
            exit_code = process.poll()
            if exit_code is not None:
                _fail(f"{specification.name} exited before readiness (code {exit_code}).")

    @staticmethod
    def _url_ready(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def wait_until_healthy(self, urls: Sequence[str], *, timeout_seconds: float) -> None:
        pending = set(urls)
        deadline = time.monotonic() + timeout_seconds
        while pending and time.monotonic() < deadline:
            self._raise_if_exited()
            pending = {url for url in pending if not self._url_ready(url)}
            if pending:
                time.sleep(0.2)
        if pending:
            _fail("Startup health check timed out: " + ", ".join(sorted(pending)))

    def wait(self) -> int:
        while True:
            for specification, process in self.processes:
                exit_code = process.poll()
                if exit_code is not None:
                    print(
                        f"[{specification.name}] exited with code {exit_code}; "
                        "stopping the process group.",
                        flush=True,
                    )
                    return exit_code if exit_code != 0 else 1
            time.sleep(0.2)

    def stop(self) -> None:
        for _, process in self.processes:
            if process.poll() is not None:
                continue
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
        deadline = time.monotonic() + 8.0
        for _, process in self.processes:
            if process.poll() is not None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
        for thread in self.threads:
            thread.join(timeout=1.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Set up and run the Harness backend, frontend, and embedded Image Agent "
            "dependencies. With no command, setup and start are executed."
        )
    )
    subparsers = parser.add_subparsers(dest="command")
    setup_parser = subparsers.add_parser("setup", help="install locked dependencies")
    setup_parser.add_argument("--force", action="store_true")

    ppt_setup_parser = subparsers.add_parser(
        "setup-ppt-runtime",
        help="install and attest locked PPT Agent dependencies for the current Python",
    )
    ppt_setup_parser.add_argument("--force", action="store_true")

    config_parser = subparsers.add_parser(
        "config-check", help="validate root YAML and environment references"
    )
    config_parser.add_argument("--root", type=Path, default=ROOT)

    doctor_parser = subparsers.add_parser("doctor", help="validate the local environment")
    doctor_parser.add_argument("--skip-ports", action="store_true")
    doctor_parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    doctor_parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)

    start_parser = subparsers.add_parser("start", help="start backend and frontend")
    start_parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    start_parser.add_argument(
        "--timeout", type=float, default=DEFAULT_STARTUP_TIMEOUT_SECONDS
    )
    start_parser.add_argument(
        "--check",
        action="store_true",
        help="stop after all startup health checks pass",
    )
    return parser


def _valid_port(value: int) -> bool:
    return 1 <= value <= 65535


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    launcher = DevelopmentLauncher()
    try:
        if args.command is None:
            launcher.setup()
            return launcher.start()
        if args.command == "setup":
            launcher.setup(force=args.force)
            return 0
        if args.command == "setup-ppt-runtime":
            launcher.install_ppt_dependencies(
                force=args.force,
                runtime_python=launcher.python,
            )
            return 0
        if args.command == "config-check":
            DevelopmentLauncher(root=args.root.resolve()).config_check()
            return 0
        if args.command == "doctor":
            if not _valid_port(args.backend_port) or not _valid_port(args.frontend_port):
                _fail("Ports must be between 1 and 65535.")
            if args.backend_port == args.frontend_port:
                _fail("Backend and frontend ports must differ.")
            launcher.doctor(
                check_ports=not args.skip_ports,
                backend_port=args.backend_port,
                frontend_port=args.frontend_port,
            )
            return 0
        if not _valid_port(args.frontend_port):
            _fail("Frontend port must be between 1 and 65535.")
        if args.timeout <= 0:
            _fail("Startup timeout must be positive.")
        return launcher.start(
            frontend_port=args.frontend_port,
            timeout_seconds=args.timeout,
            check_only=args.check,
        )
    except ConfigCheckFailed as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (LauncherError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Development launcher failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
