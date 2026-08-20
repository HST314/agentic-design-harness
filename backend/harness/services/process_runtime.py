"""Validated process specifications and persisted single-machine runtime claims."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import HarnessError
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.locks import FileLock
from ..storage.repository import utc_now
from ..storage.store import FileStateStore

ACTIVE_LAUNCH_STATES = frozenset({"PREPARED", "STARTING", "RUNNING"})
_ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_SENSITIVE_ENVIRONMENT = re.compile(
    r"(?:authorization|cookie|api[_-]?key|password|secret|token|base[_-]?url)", re.I
)


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    command: tuple[str, ...]
    agent_code_ref: str
    public_environment: dict[str, str] = field(default_factory=dict)
    health_path: str = "/healthz"
    readiness_path: str = "/readyz"
    ui_path: str = "/"

    def validate(self) -> None:
        if (
            not self.command
            or not Path(self.command[0]).is_absolute()
            or not Path(self.command[0]).is_file()
            or not self.agent_code_ref
            or any("\x00" in item for item in self.command)
        ):
            raise HarnessError("PROCESS_START_FAILED", "The process command is invalid.")
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
            "agent_code_ref": spec.agent_code_ref,
            "public_environment": spec.public_environment,
            "health_path": spec.health_path,
            "readiness_path": spec.readiness_path,
            "ui_path": spec.ui_path,
        }
    )


def process_code_identity(spec: ProcessSpec) -> str:
    executable = Path(spec.command[0]).resolve(strict=True)
    executable_stat = executable.stat()
    command_files = []
    for value in spec.command[1:]:
        candidate = Path(value)
        if not candidate.is_absolute() or not candidate.is_file():
            continue
        if candidate.is_symlink():
            raise HarnessError(
                "PROCESS_START_FAILED", "Agent code files cannot be symlinks."
            )
        command_files.append(
            {"path": str(candidate.resolve()), "sha256": file_sha256(candidate)}
        )
    return digest_json(
        {
            "agent_code_ref": spec.agent_code_ref,
            "command": list(spec.command),
            "executable": str(executable),
            "executable_size": executable_stat.st_size,
            "executable_mtime_ns": executable_stat.st_mtime_ns,
            "executable_sha256": file_sha256(executable),
            "command_files": command_files,
        }
    )


def validate_launch_identity(
    record: dict,
    task_id: str,
    instance_id: str,
    attempt_id: str,
    spec: ProcessSpec,
) -> None:
    if (
        record["task_id"] != task_id
        or record["instance_id"] != instance_id
        or record["attempt_id"] != attempt_id
        or record["code_identity"] != process_code_identity(spec)
        or record["spec_sha256"] != process_spec_digest(spec)
    ):
        raise HarnessError(
            "IDEMPOTENCY_CONFLICT",
            "The launch id was reused for a different process request.",
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind((host, port))
        except OSError:
            return False
    return True


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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


def process_start_identity(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split()
        if len(fields) < 20 or fields[0] == "Z":
            return None
        start_ticks = fields[19]
        boot_path = Path("/proc/sys/kernel/random/boot_id")
        boot_id = boot_path.read_text(encoding="utf-8").strip() if boot_path.exists() else "boot"
        return hashlib.sha256(f"{boot_id}:{start_ticks}".encode()).hexdigest()
    except (OSError, ValueError):
        return None
