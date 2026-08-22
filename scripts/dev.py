#!/usr/bin/env python3
"""Cross-platform bootstrap, diagnostics, and local process launcher."""

from __future__ import annotations

import argparse
import hashlib
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
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
VENV_ROOT = ROOT / ".venv"
IMAGE_STAMP_NAME = ".requirements-installed"
FRONTEND_STAMP_NAME = ".harness-package-lock.sha256"
DEFAULT_BACKEND_PORT = 18080
DEFAULT_FRONTEND_PORT = 18180

_IGNORED_TREE_NAMES = frozenset(
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
_IGNORED_TREE_SUFFIXES = (".egg-info", ".pyc", ".pyo")
_VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class LauncherError(RuntimeError):
    """An actionable local environment or child-process failure."""


def _fail(message: str) -> NoReturn:
    raise LauncherError(message)


def _console_safe(value: str, *, encoding: str | None = None) -> str:
    """Make child output printable even on a legacy Windows console encoding."""

    selected_encoding = encoding or getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        value.encode(selected_encoding)
    except UnicodeEncodeError:
        return value.encode(selected_encoding, errors="backslashreplace").decode(
            selected_encoding
        )
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def input_digest(
    paths: Sequence[Path], *, identity: Sequence[str] = (), root: Path = ROOT
) -> str:
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

    manifest: list[dict[str, str | int]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if any(
            part in _IGNORED_TREE_NAMES or part.endswith(_IGNORED_TREE_SUFFIXES)
            for part in relative.parts
        ):
            continue
        if relative.parts and relative.parts[0] in {"bin", "Scripts"}:
            continue
        if path.is_symlink():
            _fail(f"Dependency installation contains an unsafe entry: {relative}")
        if path.name == "RECORD" or path.is_dir():
            continue
        if not path.is_file():
            _fail(f"Dependency installation contains an unsafe entry: {relative}")
        manifest.append(
            {
                "path": relative.as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    def frontend_root(self) -> Path:
        return self.root / "frontend"

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
        relative = self.image_agent_root.relative_to(self.root).as_posix()
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

    def backend_input_digest(self) -> str:
        return input_digest(
            [self.root / "requirements-runtime.txt", self.root / "pyproject.toml"],
            identity=(
                f"python={sys.implementation.cache_tag}",
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
        except LauncherError:
            return False
        if current.get("input_sha256") != self.backend_input_digest():
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
        if self.venv_python.is_file() and command_succeeds(
            [self.venv_python, "-c", "import sys; assert sys.prefix != sys.base_prefix"]
        ):
            return
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
            if not self.venv_python.is_file() or not command_succeeds(
                [
                    self.venv_python,
                    "-c",
                    "import sys; assert sys.prefix != sys.base_prefix",
                ]
            ):
                _fail("Python could not create a usable virtual environment.")
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

    def pip_installer(self) -> tuple[list[str], Path | None]:
        if command_succeeds([self.venv_python, "-m", "pip", "--version"]):
            return [str(self.venv_python), "-m", "pip"], None
        if not command_succeeds([self.python, "-m", "pip", "--version"]):
            _fail(
                "pip is unavailable. Install the Python pip/venv components and rerun setup."
            )
        if os.name == "nt":
            site_packages = self.venv_root / "Lib" / "site-packages"
        else:
            site_packages = (
                self.venv_root
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
        site_packages.mkdir(parents=True, exist_ok=True)
        return [str(self.python), "-m", "pip"], site_packages

    def install_backend(self, *, force: bool = False) -> None:
        self.ensure_virtual_environment()
        if not force and self.backend_is_current():
            print("[ok] Harness Python environment matches its lock.", flush=True)
            return
        installer, external_target = self.pip_installer()
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
        write_json(
            self.venv_root / ".harness-runtime.json",
            {
                "schema_version": "1.0",
                "input_sha256": self.backend_input_digest(),
                "python": sys.implementation.cache_tag,
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

    def image_input_digest(self) -> str:
        lock = load_json(self.lock_path)
        paths: list[Path] = [self.lock_path]
        dependencies = lock.get("dependencies")
        if not isinstance(dependencies, dict) or not isinstance(
            dependencies.get("files"), list
        ):
            _fail("Image Agent lock has no dependency file list.")
        for item in dependencies["files"]:
            if not isinstance(item, dict):
                _fail("Image Agent lock contains an invalid dependency record.")
            scope = item.get("scope")
            relative = Path(str(item.get("path", "")))
            paths.append(
                (self.root if scope == "harness" else self.image_agent_root) / relative
            )
        return input_digest(
            paths,
            identity=(
                f"python={sys.implementation.cache_tag}",
                f"platform={sys.platform}",
                f"machine={platform.machine().lower()}",
            ),
            root=self.root,
        )

    def image_environment(self, dependency_root: Path | None = None) -> dict[str, str]:
        return _pythonpath(
            os.environ.copy(),
            dependency_root or self.image_dependency_root,
            self.image_agent_root,
        )

    def image_dependencies_are_importable(
        self, dependency_root: Path | None = None
    ) -> bool:
        return command_succeeds(
            [
                self.venv_python,
                "-c",
                "import fastapi, httpx, openai, PIL, portalocker, pydantic, uvicorn, yaml",
            ],
            cwd=self.image_agent_root,
            environment=self.image_environment(dependency_root),
        )

    def image_dependencies_are_current(self) -> bool:
        root = self.image_dependency_root
        stamp_path = root / IMAGE_STAMP_NAME
        if not root.is_dir() or not stamp_path.is_file():
            return False
        try:
            stamp = load_json(stamp_path)
            actual_digest = content_tree_digest(root)
        except (LauncherError, OSError):
            return False
        return (
            stamp.get("input_sha256") == self.image_input_digest()
            and stamp.get("content_sha256") == actual_digest
            and self.image_dependencies_are_importable()
        )

    def install_image_dependencies(self, *, force: bool = False) -> None:
        if not force and self.image_dependencies_are_current():
            print("[ok] Image Agent isolated dependencies match their locks.", flush=True)
            return
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".image-agent-deps-", dir=self.runtime_root)
        )
        backup: Path | None = None
        try:
            installer, _ = self.pip_installer()
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
            if not self.image_dependencies_are_importable(temporary):
                _fail("Image Agent dependencies installed, but required imports fail.")
            content_sha256 = content_tree_digest(temporary)
            write_json(
                temporary / IMAGE_STAMP_NAME,
                {
                    "schema_version": "1.0",
                    "input_sha256": self.image_input_digest(),
                    "content_sha256": content_sha256,
                    "python": sys.implementation.cache_tag,
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

    def frontend_input_digest(self) -> str:
        return input_digest(
            [self.frontend_root / "package-lock.json"], root=self.root
        )

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
        self.install_frontend(force=force)
        print("[ok] Development environment is ready.", flush=True)

    def _check_configuration(self) -> None:
        environment = self.backend_environment()
        environment.update(
            {
                "HARNESS_IMAGE_AGENT_ROOT": str(self.image_agent_root),
                "HARNESS_IMAGE_AGENT_PATH_MODE": "embedded_only",
                "HARNESS_IMAGE_AGENT_PYTHON": str(self.venv_python),
                "HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT": str(
                    self.image_dependency_root
                ),
            }
        )
        code = (
            "from pathlib import Path; from harness.core.config import load_settings; "
            f"s=load_settings(Path({str(self.root)!r})); "
            "assert s.contracts_root.is_dir(); assert s.image_agent_root.is_dir(); "
            "assert s.image_agent_lock_path.is_file(); "
            "assert s.image_agent_dependency_root.is_dir()"
        )
        if not command_succeeds(
            [self.venv_python, "-c", code],
            cwd=self.root,
            environment=environment,
        ):
            _fail("Harness configuration paths are incomplete or invalid.")

    def _check_writable_runtime(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".doctor-", dir=self.runtime_root
            )
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
    ) -> None:
        self.check_tools()
        self.verify_image_lock()
        if not self.backend_is_current():
            _fail("Harness Python environment is missing or stale; run scripts/dev.py setup.")
        print("[ok] Harness Python environment", flush=True)
        if not self.image_dependencies_are_current():
            _fail("Image Agent dependencies are missing or stale; run scripts/dev.py setup.")
        print("[ok] Image Agent isolated dependency environment", flush=True)
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
        print("[ok] Doctor completed without errors.", flush=True)

    def start(
        self,
        *,
        backend_port: int = DEFAULT_BACKEND_PORT,
        frontend_port: int = DEFAULT_FRONTEND_PORT,
        timeout_seconds: float = 45.0,
        check_only: bool = False,
    ) -> int:
        self.doctor(
            check_ports=True,
            backend_port=backend_port,
            frontend_port=frontend_port,
        )
        backend_environment = self.backend_environment()
        backend_environment.update(
            {
                "HARNESS_HOST": "127.0.0.1",
                "HARNESS_PORT": str(backend_port),
                "HARNESS_IMAGE_AGENT_ROOT": str(self.image_agent_root),
                "HARNESS_IMAGE_AGENT_PATH_MODE": "embedded_only",
                "HARNESS_IMAGE_AGENT_PYTHON": str(self.venv_python),
                "HARNESS_IMAGE_AGENT_DEPENDENCY_ROOT": str(
                    self.image_dependency_root
                ),
            }
        )
        frontend_environment = self.npm_environment()
        frontend_environment["HARNESS_BACKEND_URL"] = (
            f"http://127.0.0.1:{backend_port}"
        )
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

    def wait_until_healthy(
        self, urls: Sequence[str], *, timeout_seconds: float
    ) -> None:
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

    doctor_parser = subparsers.add_parser("doctor", help="validate the local environment")
    doctor_parser.add_argument("--skip-ports", action="store_true")
    doctor_parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    doctor_parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)

    start_parser = subparsers.add_parser("start", help="start backend and frontend")
    start_parser.add_argument("--backend-port", type=int, default=DEFAULT_BACKEND_PORT)
    start_parser.add_argument("--frontend-port", type=int, default=DEFAULT_FRONTEND_PORT)
    start_parser.add_argument("--timeout", type=float, default=45.0)
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
        if not _valid_port(args.backend_port) or not _valid_port(args.frontend_port):
            _fail("Ports must be between 1 and 65535.")
        if args.backend_port == args.frontend_port:
            _fail("Backend and frontend ports must differ.")
        if args.timeout <= 0:
            _fail("Startup timeout must be positive.")
        return launcher.start(
            backend_port=args.backend_port,
            frontend_port=args.frontend_port,
            timeout_seconds=args.timeout,
            check_only=args.check,
        )
    except (LauncherError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Development launcher failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
