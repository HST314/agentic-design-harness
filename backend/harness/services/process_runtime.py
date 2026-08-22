"""Validated process specifications and persisted single-machine runtime claims."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.safe_open import is_link_or_reparse, open_regular_readonly
from ..storage.store import FileStateStore
from .process_control import process_tree_exists

ACTIVE_LAUNCH_STATES = frozenset({"PREPARED", "STARTING", "RUNNING"})
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SENSITIVE_ENVIRONMENT = re.compile(
    r"(?:authorization|cookie|api[_-]?key|password|secret|token|base[_-]?url)", re.I
)
_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True, slots=True)
class AgentRuntimeArtifact:
    """One immutable Agent source/package and dependency revision."""

    artifact_id: str
    revision: str
    source_root: Path
    entrypoint_relpath: str
    dependency_lock_relpaths: tuple[str, ...]
    environment_root: Path | None = None

    def validate(self) -> None:
        if not _ARTIFACT_NAME.fullmatch(self.artifact_id) or not _ARTIFACT_NAME.fullmatch(
            self.revision
        ):
            _artifact_invalid("The runtime artifact id or revision is invalid.")
        if (
            not self.source_root.is_absolute()
            or (self.source_root.exists() and is_link_or_reparse(self.source_root))
        ):
            _artifact_invalid("The runtime artifact root must be an absolute directory.")
        try:
            resolved_root = self.source_root.resolve(strict=True)
            if not self.source_root.is_dir() or (
                os.name != "nt" and resolved_root != self.source_root
            ):
                _artifact_invalid("The runtime artifact root does not exist.")
        except OSError:
            _artifact_invalid("The runtime artifact root cannot be inspected.")
        _artifact_relative_path(self.entrypoint_relpath)
        if not self.dependency_lock_relpaths:
            _artifact_invalid("At least one dependency lock belongs to every runtime artifact.")
        if len(self.dependency_lock_relpaths) != len(set(self.dependency_lock_relpaths)):
            _artifact_invalid("Runtime artifact dependency locks must be unique.")
        for relative_path in self.dependency_lock_relpaths:
            _artifact_relative_path(relative_path)
        if self.environment_root is not None and (
            not self.environment_root.is_absolute()
            or (
                self.environment_root.exists()
                and is_link_or_reparse(self.environment_root)
            )
            or not self.environment_root.is_dir()
        ):
            _artifact_invalid("The runtime environment root is invalid.")


@dataclass(frozen=True, slots=True)
class AgentRuntimeIdentity:
    artifact_id: str
    revision: str
    source_root: str
    source_root_identity: dict[str, int]
    entrypoint_relpath: str
    source_manifest: tuple[dict[str, Any], ...]
    source_manifest_sha256: str
    dependency_locks_sha256: str
    interpreter: dict[str, Any]
    environment: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "source_root": self.source_root,
            "source_root_identity": self.source_root_identity,
            "entrypoint_relpath": self.entrypoint_relpath,
            "source_manifest": list(self.source_manifest),
            "source_manifest_sha256": self.source_manifest_sha256,
            "dependency_locks_sha256": self.dependency_locks_sha256,
            "interpreter": self.interpreter,
            "environment": self.environment,
        }

    @property
    def digest(self) -> str:
        return digest_json(self.as_dict())


@dataclass(slots=True)
class PinnedRuntimeArtifact:
    """An inspected artifact root held open through process creation."""

    identity: AgentRuntimeIdentity
    source_descriptor: int | None
    windows_root_handle: int | None
    source_root: Path
    entrypoint_relpath: str

    def verify_current(self) -> None:
        """Fail before side effects if a portable path-backed pin has changed."""

        if self.source_descriptor is None:
            current_manifest = tuple(_artifact_manifest(self.source_root))
            if digest_json(list(current_manifest)) != self.identity.source_manifest_sha256:
                _artifact_invalid("The runtime artifact changed before process creation.")

    def execution_command(self, command: list[str]) -> list[str]:
        configured_entrypoint = str(
            self.source_root / PurePosixPath(self.entrypoint_relpath)
        )
        if self.source_descriptor is None:
            self.verify_current()
            if configured_entrypoint not in command:
                _artifact_invalid("The process command does not use the artifact entrypoint.")
            return command
        descriptor_entrypoint = (
            f"/proc/self/fd/{self.source_descriptor}/{self.entrypoint_relpath}"
        )
        rewritten = [
            descriptor_entrypoint if item == configured_entrypoint else item
            for item in command
        ]
        if rewritten == command:
            _artifact_invalid("The process command does not use the artifact entrypoint.")
        return rewritten

    def close(self) -> None:
        if self.source_descriptor is not None and self.source_descriptor >= 0:
            os.close(self.source_descriptor)
            self.source_descriptor = None
        if self.windows_root_handle is not None:
            _close_windows_handle(self.windows_root_handle)
            self.windows_root_handle = None

    def __enter__(self) -> PinnedRuntimeArtifact:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    command: tuple[str, ...]
    runtime_artifact: AgentRuntimeArtifact
    public_environment: dict[str, str] = field(default_factory=dict)
    health_path: str = "/healthz"
    readiness_path: str = "/readyz"
    ui_path: str = "/"

    def validate(self) -> None:
        if (
            not self.command
            or not Path(self.command[0]).is_absolute()
            or not Path(self.command[0]).is_file()
            or any("\x00" in item for item in self.command)
        ):
            raise HarnessError("PROCESS_START_FAILED", "The process command is invalid.")
        self.runtime_artifact.validate()
        entrypoint = (
            self.runtime_artifact.source_root
            / PurePosixPath(self.runtime_artifact.entrypoint_relpath)
        )
        if sum(item == str(entrypoint) for item in self.command[1:]) != 1:
            _artifact_invalid("The process command does not use the artifact entrypoint.")
        for name, value in self.public_environment.items():
            if (
                not _ENVIRONMENT_NAME.fullmatch(name)
                or _SENSITIVE_ENVIRONMENT.search(name)
                or "\x00" in value
            ):
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The public process environment contains a forbidden field.",
                    {"environment_name": name},
                )
        for path in (self.health_path, self.readiness_path, self.ui_path):
            if not path.startswith("/") or ".." in path or "\x00" in path:
                raise HarnessError("PROCESS_START_FAILED", "A process URL path is invalid.")


def process_spec_digest(spec: ProcessSpec) -> str:
    return digest_json(
        {
            "command": list(spec.command),
            "runtime_artifact": {
                "artifact_id": spec.runtime_artifact.artifact_id,
                "revision": spec.runtime_artifact.revision,
                "source_root": str(spec.runtime_artifact.source_root),
                "entrypoint_relpath": spec.runtime_artifact.entrypoint_relpath,
                "dependency_lock_relpaths": list(
                    spec.runtime_artifact.dependency_lock_relpaths
                ),
                "environment_root": (
                    str(spec.runtime_artifact.environment_root)
                    if spec.runtime_artifact.environment_root is not None
                    else None
                ),
            },
            "public_environment": spec.public_environment,
            "health_path": spec.health_path,
            "readiness_path": spec.readiness_path,
            "ui_path": spec.ui_path,
        }
    )


def process_code_identity(spec: ProcessSpec) -> str:
    return runtime_artifact_identity(spec).digest


def runtime_artifact_identity(spec: ProcessSpec) -> AgentRuntimeIdentity:
    """Inspect a complete immutable artifact through no-follow descriptors."""

    with pin_runtime_artifact(spec) as pinned:
        return pinned.identity


def pin_runtime_artifact(spec: ProcessSpec) -> PinnedRuntimeArtifact:
    """Pin the inspected source root so pathname swaps cannot change execution."""

    spec.validate()
    artifact = spec.runtime_artifact
    descriptor: int | None = None
    windows_root_handle: int | None = None
    try:
        try:
            if os.name == "nt":
                windows_root_handle = _open_windows_directory_guard(
                    artifact.source_root
                )
                root_stat = artifact.source_root.lstat()
                if is_link_or_reparse(artifact.source_root, root_stat):
                    _artifact_invalid("The runtime artifact root cannot be opened safely.")
                manifest = tuple(_artifact_manifest(artifact.source_root))
            else:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                descriptor = os.open(artifact.source_root, flags)
                root_stat = os.fstat(descriptor)
                manifest = tuple(_artifact_manifest(descriptor))
        except OSError:
            _artifact_invalid("The runtime artifact root cannot be opened safely.")
        if not stat.S_ISDIR(root_stat.st_mode):
            _artifact_invalid("The runtime artifact root cannot be opened safely.")
        if _artifact_entry_is_writable(root_stat):
            _artifact_invalid("The runtime artifact root must be read-only.")
        manifest_by_path = {item["path"]: item for item in manifest}
        required_paths = {artifact.entrypoint_relpath, *artifact.dependency_lock_relpaths}
        if not required_paths.issubset(manifest_by_path):
            _artifact_invalid("The artifact entrypoint or dependency lock is missing.")
        locks = [manifest_by_path[item] for item in sorted(artifact.dependency_lock_relpaths)]
        executable = Path(spec.command[0]).resolve(strict=True)
        environment_root = artifact.environment_root or executable.parent.parent
        environment_config = environment_root / "pyvenv.cfg"
        environment = {
            "root": str(environment_root.resolve(strict=True)),
            "pyvenv_config_sha256": (
                file_sha256(environment_config) if environment_config.is_file() else None
            ),
        }
        interpreter_stat = executable.stat()
        identity = AgentRuntimeIdentity(
            artifact_id=artifact.artifact_id,
            revision=artifact.revision,
            source_root=str(artifact.source_root),
            source_root_identity={
                "device": root_stat.st_dev,
                "inode": root_stat.st_ino,
            },
            entrypoint_relpath=artifact.entrypoint_relpath,
            source_manifest=manifest,
            source_manifest_sha256=digest_json(list(manifest)),
            dependency_locks_sha256=digest_json(locks),
            interpreter={
                "path": str(executable),
                "size_bytes": interpreter_stat.st_size,
                "sha256": file_sha256(executable),
            },
            environment=environment,
        )
        return PinnedRuntimeArtifact(
            identity=identity,
            source_descriptor=descriptor,
            windows_root_handle=windows_root_handle,
            source_root=artifact.source_root,
            entrypoint_relpath=artifact.entrypoint_relpath,
        )
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if windows_root_handle is not None:
            _close_windows_handle(windows_root_handle)
        raise


def _artifact_manifest(root: int | Path) -> list[dict[str, Any]]:
    manifest: list[dict[str, Any]] = []
    if isinstance(root, int):
        _walk_artifact(root, PurePosixPath(), manifest)
    else:
        _walk_artifact_path(root, PurePosixPath(), manifest)
    if not manifest:
        _artifact_invalid("The runtime artifact cannot be empty.")
    return manifest


def _walk_artifact(
    directory_fd: int,
    prefix: PurePosixPath,
    manifest: list[dict[str, Any]],
) -> None:
    try:
        with os.scandir(directory_fd) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        _artifact_invalid("The runtime artifact directory cannot be enumerated.")
    for name in names:
        if name in {".", ".."}:
            continue
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            child_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError:
            _artifact_invalid("The runtime artifact contains a symlink or unreadable entry.")
        try:
            before = os.fstat(child_fd)
            relative = prefix / name
            if _artifact_entry_is_writable(before):
                _artifact_invalid("Every runtime artifact entry must be read-only.")
            if stat.S_ISDIR(before.st_mode):
                _walk_artifact(child_fd, relative, manifest)
                continue
            if not stat.S_ISREG(before.st_mode):
                _artifact_invalid("Runtime artifacts may contain only directories and files.")
            digest = hashlib.sha256()
            with os.fdopen(os.dup(child_fd), "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            after = os.fstat(child_fd)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                _artifact_invalid("A runtime artifact file changed during inspection.")
            manifest.append(
                {
                    "path": relative.as_posix(),
                    "device": before.st_dev,
                    "inode": before.st_ino,
                    "size_bytes": before.st_size,
                    "mode": stat.S_IMODE(before.st_mode),
                    "sha256": digest.hexdigest(),
                }
            )
        finally:
            os.close(child_fd)


def _walk_artifact_path(
    root: Path,
    prefix: PurePosixPath,
    manifest: list[dict[str, Any]],
) -> None:
    current = root / Path(*prefix.parts)
    try:
        entries = sorted(os.scandir(current), key=lambda entry: entry.name)
    except OSError:
        _artifact_invalid("The runtime artifact directory cannot be enumerated.")
    for entry in entries:
        path = Path(entry.path)
        relative = prefix / entry.name
        try:
            before = entry.stat(follow_symlinks=False)
        except OSError as exc:
            _artifact_path_os_error(relative, "stat", exc)
        if is_link_or_reparse(path, before):
            _artifact_invalid(
                "The runtime artifact contains a symlink or unreadable entry."
            )
        if _artifact_entry_is_writable(before):
            _artifact_invalid("Every runtime artifact entry must be read-only.")
        if stat.S_ISDIR(before.st_mode):
            _walk_artifact_path(root, relative, manifest)
            continue
        if not stat.S_ISREG(before.st_mode):
            _artifact_invalid("Runtime artifacts may contain only directories and files.")
        try:
            descriptor = open_regular_readonly(path, trusted_root=root)
        except OSError as exc:
            _artifact_path_os_error(relative, "open", exc)
        try:
            opened = os.fstat(descriptor)
            digest = hashlib.sha256()
            with os.fdopen(os.dup(descriptor), "rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            after = os.fstat(descriptor)
        except OSError as exc:
            _artifact_path_os_error(relative, "read", exc)
        finally:
            os.close(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            _artifact_invalid("A runtime artifact file changed during inspection.")
        manifest.append(
            {
                "path": relative.as_posix(),
                "device": opened.st_dev,
                "inode": opened.st_ino,
                "size_bytes": opened.st_size,
                "mode": stat.S_IMODE(opened.st_mode),
                "sha256": digest.hexdigest(),
            }
        )


def _artifact_path_os_error(
    relative: PurePosixPath, operation: str, error: OSError
) -> NoReturn:
    details: dict[str, str | int] = {
        "path": relative.as_posix(),
        "operation": operation,
    }
    if error.errno is not None:
        details["errno"] = error.errno
    winerror = getattr(error, "winerror", None)
    if isinstance(winerror, int):
        details["winerror"] = winerror
    raise HarnessError(
        "PROCESS_START_FAILED",
        "The runtime artifact contains a symlink or unreadable entry.",
        details,
    ) from None


def _open_windows_directory_guard(path: Path) -> int:
    """Hold a Windows directory open without delete sharing during launch."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0,
        0x00000001 | 0x00000002,  # share reads and writes, but not deletion
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # directory handle, do not follow reparse points
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        last_error = ctypes.get_last_error()  # type: ignore[attr-defined]
        raise OSError(last_error, f"cannot guard runtime directory: {path}")
    return handle


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _artifact_relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        _artifact_invalid("A runtime artifact path is invalid.")
    return candidate


def _artifact_entry_is_writable(metadata: os.stat_result) -> bool:
    if os.name == "nt":
        if stat.S_ISDIR(metadata.st_mode):
            # The DOS read-only bit on directories does not enforce directory
            # immutability. Runtime file hashes are the authoritative boundary.
            return False
        readonly_flag = getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1)
        return not bool(getattr(metadata, "st_file_attributes", 0) & readonly_flag)
    return bool(metadata.st_mode & 0o222)


def _artifact_invalid(message: str) -> NoReturn:
    raise HarnessError("PROCESS_START_FAILED", message)


def validate_launch_identity(
    record: dict,
    task_id: str,
    instance_id: str,
    attempt_id: str,
    spec: ProcessSpec,
    *,
    runtime_identity: AgentRuntimeIdentity | None = None,
) -> None:
    code_identity = (
        runtime_identity.digest
        if runtime_identity is not None
        else process_code_identity(spec)
    )
    if (
        record["task_id"] != task_id
        or record["instance_id"] != instance_id
        or record["attempt_id"] != attempt_id
        or record["code_identity"] != code_identity
        or record["spec_sha256"] != process_spec_digest(spec)
    ):
        raise HarnessError(
            "IDEMPOTENCY_CONFLICT",
            "The launch id was reused for a different process request.",
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        if os.name == "nt":
            candidate = path.resolve(strict=True)
            metadata = candidate.lstat()
            if is_link_or_reparse(candidate, metadata) or not stat.S_ISREG(
                metadata.st_mode
            ):
                raise OSError("runtime dependency is not a regular file")
            descriptor = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        else:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        with os.fdopen(descriptor, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        raise HarnessError(
            "PROCESS_START_FAILED", "An Agent code file could not be pinned."
        ) from None
    return digest.hexdigest()


def reject_credential_arguments(
    command: list[str], api_key: str, base_url: str
) -> None:
    if any(api_key in item or base_url in item for item in command):
        raise HarnessError(
            "CREDENTIAL_PAIR_INVALID",
            "Credentials must be injected through the controlled environment pair.",
        )


class PortAllocator:
    def __init__(self, store: FileStateStore, host: str) -> None:
        self.store = store
        self.host = host
        self.path = store.layout.control_root / "processes" / "ports.json"
        self.lock_path = store.layout.control_root / "locks" / "ports.lock"

    def allocate(
        self,
        task_id: str,
        instance_id: str,
        launch_id: str,
        start: int,
        end: int,
    ) -> int:
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            state = self._read()
            for port in range(start, end + 1):
                if str(port) in state["claims"] or not port_is_available(self.host, port):
                    continue
                state["claims"][str(port)] = {
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "launch_id": launch_id,
                    "claimed_at": utc_now(),
                }
                atomic_write_json(self.path, state)
                return port
        raise HarnessError(
            "PROCESS_START_FAILED",
            "No free port is available in the configured range.",
            {"port_range_start": start, "port_range_end": end},
        )

    def restore(self, task_id: str, instance_id: str, launch_id: str, port: int) -> None:
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            state = self._read()
            current = state["claims"].get(str(port))
            expected = {
                "task_id": task_id,
                "instance_id": instance_id,
                "launch_id": launch_id,
            }
            if current is not None and any(
                current[key] != value for key, value in expected.items()
            ):
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "A live launch conflicts with the persisted port claim.",
                    {"port": port},
                )
            if current is None:
                state["claims"][str(port)] = {**expected, "claimed_at": utc_now()}
                atomic_write_json(self.path, state)

    def release(self, launch_id: str, port: int) -> None:
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            state = self._read()
            current = state["claims"].get(str(port))
            if current is not None and current["launch_id"] == launch_id:
                del state["claims"][str(port)]
                atomic_write_json(self.path, state)

    def reconcile(self, active_launches: dict[str, dict[str, object]]) -> None:
        with FileLock(self.lock_path, self.store.lock_timeout_seconds):
            state = self._read()
            retained = {}
            for port, claim in state["claims"].items():
                launch = active_launches.get(claim["launch_id"])
                if (
                    launch is not None
                    and launch.get("port") == int(port)
                    and launch.get("state") in ACTIVE_LAUNCH_STATES
                ):
                    retained[port] = claim
            if retained != state["claims"]:
                state["claims"] = retained
                atomic_write_json(self.path, state)

    def _read(self) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            return {"schema_version": "1.0", "claims": {}}
        return read_json(self.path)


def port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        if os.name != "nt":
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind((host, port))
        except OSError:
            return False
    return True


def process_group_exists(process_group_id: int) -> bool:
    return process_tree_exists(process_group_id)


def tail_lines(
    path: Path,
    max_lines: int,
    max_bytes: int = 1024 * 1024,
    *,
    trusted_root: Path | None = None,
) -> list[str]:
    try:
        descriptor = _open_regular_nofollow(path, trusted_root=trusted_root)
    except FileNotFoundError:
        return []
    with os.fdopen(descriptor, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        raw = handle.read(max_bytes)
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def _open_regular_nofollow(path: Path, *, trusted_root: Path | None) -> int:
    if os.name == "nt":
        try:
            return open_regular_readonly(path, trusted_root=trusted_root)
        except FileNotFoundError:
            raise
        except OSError:
            raise HarnessError(
                "PATH_OUTSIDE_TASK_ROOT",
                "The log path is not a regular file beneath the workspace root.",
            ) from None
    if trusted_root is None:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
    else:
        try:
            relative = path.relative_to(trusted_root)
        except ValueError:
            raise HarnessError(
                "PATH_OUTSIDE_TASK_ROOT", "The log path leaves the workspace root."
            ) from None
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptors: list[int] = []
        try:
            current = os.open(trusted_root, directory_flags)
            descriptors.append(current)
            for component in relative.parts[:-1]:
                current = os.open(component, directory_flags, dir_fd=current)
                descriptors.append(current)
            descriptor = os.open(
                relative.parts[-1],
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
        except FileNotFoundError:
            raise
        except OSError:
            raise HarnessError(
                "PATH_OUTSIDE_TASK_ROOT",
                "Symlinked or invalid log path components are forbidden.",
            ) from None
        finally:
            for opened in reversed(descriptors):
                os.close(opened)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise HarnessError(
            "PATH_OUTSIDE_TASK_ROOT", "Only regular no-follow log files may be read."
        )
    return descriptor
