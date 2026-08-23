"""Single-machine process groups, port ownership and crash reconciliation."""
from __future__ import annotations

import http.client
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import HarnessError
from ..domain.service import TaskCommandService
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.ndjson import append_record
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .agent_config_materialization import (
    ImageAgentConfigMaterializer,
    ImageAgentLaunchConfiguration,
)
from .process_control import (
    process_group_contains,
    process_start_identity,
    terminate_process_tree,
    wrapper_spawn_options,
)
from .process_runtime import (
    ACTIVE_LAUNCH_STATES,
    PinnedRuntimeArtifact,
    PortAllocator,
    ProcessSpec,
    pin_runtime_artifact,
    process_spec_digest,
    reject_credential_arguments,
    validate_launch_identity,
)
from .supervisor_lifecycle import SupervisorLifecycleMixin

_INHERITED_ENVIRONMENT = {
    "LANG",
    "LC_ALL",
    "PATH",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "TZ",
    "WINDIR",
}


class ProcessSupervisor(SupervisorLifecycleMixin):
    """Starts one persistent redacting wrapper process for each Agent instance."""

    def __init__(
        self,
        store: FileStateStore,
        commands: TaskCommandService,
        image_config: ImageAgentConfigMaterializer,
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self.store = store
        self.commands = commands
        self.image_config = image_config
        self.host = host
        self.process_root = store.layout.control_root / "processes"
        self.launch_root = self.process_root / "launches"
        self.launch_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.port_allocator = PortAllocator(store, host)
        self._children: dict[str, subprocess.Popen[Any]] = {}
        self._lock_registry_guard = threading.Lock()
        self._instance_locks: dict[tuple[str, str], threading.RLock] = {}
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self.logger = logging.getLogger("harness.process_supervisor")

    def start_instance(
        self,
        task_id: str,
        instance_id: str,
        spec: ProcessSpec,
        *,
        launch_id: str,
        attempt_id: str,
        crash_hook=None,
    ) -> dict[str, Any]:
        return self._start(
            task_id,
            instance_id,
            spec,
            launch_id=launch_id,
            attempt_id=attempt_id,
            preserve_business_state=False,
            crash_hook=crash_hook,
        )

    def restart_instance(
        self,
        task_id: str,
        instance_id: str,
        spec: ProcessSpec,
        *,
        launch_id: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        with self._instance_thread_lock(task_id, instance_id):
            instance = self._instance(task_id, instance_id)
            preserve = instance["status"] in {"RUNNING", "WAITING_APPROVAL"}
            with pin_runtime_artifact(spec) as pinned_artifact:
                self._enforce_code_identity(
                    task_id, instance_id, pinned_artifact.identity.digest
                )
                pinned_artifact.verify_current()
                record_path = self._launch_path(launch_id)
                if record_path.exists():
                    existing = read_json(record_path)
                    validate_launch_identity(
                        existing,
                        task_id,
                        instance_id,
                        attempt_id,
                        spec,
                        runtime_identity=pinned_artifact.identity,
                    )
                    return deepcopy(self._hydrate_launch_identity(existing))
                current = self._active_launch_for_instance(task_id, instance_id)
                if current is not None:
                    self._interrupt_model_calls(task_id, instance_id, "controlled_restart")
                    self._stop_launch(current, "RESTARTED")
                return self._start(
                    task_id,
                    instance_id,
                    spec,
                    launch_id=launch_id,
                    attempt_id=attempt_id,
                    preserve_business_state=preserve,
                    pinned_artifact=pinned_artifact,
                )

    def _start(
        self,
        task_id: str,
        instance_id: str,
        spec: ProcessSpec,
        *,
        launch_id: str,
        attempt_id: str,
        preserve_business_state: bool,
        crash_hook=None,
        pinned_artifact: PinnedRuntimeArtifact | None = None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        validate_identifier(launch_id, "launch_id")
        validate_identifier(attempt_id, "attempt_id")
        if pinned_artifact is None:
            with pin_runtime_artifact(spec) as inspected_artifact:
                return self._start(
                    task_id,
                    instance_id,
                    spec,
                    launch_id=launch_id,
                    attempt_id=attempt_id,
                    preserve_business_state=preserve_business_state,
                    crash_hook=crash_hook,
                    pinned_artifact=inspected_artifact,
                )
        lock_path = (
            self.store.layout.control_root
            / "locks"
            / f"process-{task_id}-{instance_id}.lock"
        )
        with self._instance_thread_lock(task_id, instance_id), FileLock(
            lock_path, self.store.lock_timeout_seconds
        ):
            record_path = self._launch_path(launch_id)
            if record_path.exists():
                existing = read_json(record_path)
                validate_launch_identity(
                    existing,
                    task_id,
                    instance_id,
                    attempt_id,
                    spec,
                    runtime_identity=pinned_artifact.identity,
                )
                existing = self._hydrate_launch_identity(existing)
                return deepcopy(existing)
            active = self._active_launch_for_instance(task_id, instance_id)
            if active is not None:
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The instance already has an active launch.",
                    {"launch_id": active["launch_id"]},
                )
            instance = self._instance(task_id, instance_id)
            if instance["status"] == "ARCHIVED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION", "An archived instance is read-only."
                )
            if not preserve_business_state:
                self._validate_start_eligibility(task_id, instance)
            if instance["agent_type"] != "image":
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE",
                    "Only Image Agent process materialization is available in this release.",
                )
            launch_config = self.image_config.resolve_launch(task_id, instance_id)
            runtime_identity = pinned_artifact.identity
            code_identity = runtime_identity.digest
            self._enforce_code_identity(task_id, instance_id, code_identity)
            supervisor = launch_config.supervisor
            port = self.port_allocator.allocate(
                task_id,
                instance_id,
                launch_id,
                supervisor.port_range_start,
                supervisor.port_range_end,
            )
            prepared = {
                "schema_version": "1.0",
                "task_id": task_id,
                "instance_id": instance_id,
                "launch_id": launch_id,
                "attempt_id": attempt_id,
                "state": "PREPARED",
                "port": port,
                "host": self.host,
                "command_sha256": digest_json(list(spec.command)),
                "spec_sha256": process_spec_digest(spec),
                "code_identity": code_identity,
                "runtime_artifact": runtime_identity.as_dict(),
                "health_path": spec.health_path,
                "readiness_path": spec.readiness_path,
                "ui_path": spec.ui_path,
                "prepared_at": utc_now(),
                "pid": None,
                "start_identity": None,
                "started_at": None,
                "exit_code": None,
                "health_failures": 0,
                "startup_timeout_seconds": supervisor.startup_timeout_seconds,
                "shutdown_grace_seconds": supervisor.shutdown_grace_seconds,
                "source_config_revision": launch_config.source_config_revision,
                "config_hash": launch_config.config_hash,
            }
            atomic_write_json(record_path, prepared)
            if not preserve_business_state:
                self._transition(task_id, instance_id, "STARTING", f"{launch_id}-starting")
            process: subprocess.Popen[Any] | None = None
            launch_spec_path: Path | None = None
            try:
                command = [
                    item.replace("{port}", str(port)).replace("{host}", self.host)
                    for item in spec.command
                ]
                command = pinned_artifact.execution_command(command)
                reject_credential_arguments(command, *launch_config.redaction_values)
                environment = self._child_environment(
                    task_id, instance_id, port, spec, launch_config
                )
                instance_root = self.store.layout.initialize_instance(task_id, instance_id)
                launch_spec_path = instance_root / "runtime" / f".{launch_id}.launch-spec.json"
                stdout_path = instance_root / "logs" / "stdout.log"
                stderr_path = instance_root / "logs" / "stderr.log"
                handshake_path = instance_root / "runtime" / f"{launch_id}.child.json"
                atomic_write_json(
                    launch_spec_path,
                    {
                        "command": command,
                        "cwd": str(instance_root),
                        "environment": environment,
                        "secret_environment_names": sorted(
                            launch_config.provider_environment
                        ),
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                        "handshake_path": str(handshake_path),
                        "inherited_fds": (
                            [pinned_artifact.source_descriptor]
                            if pinned_artifact.source_descriptor is not None
                            else []
                        ),
                    },
                    mode=0o600,
                )
                wrapper = Path(__file__).resolve().parents[1] / "process_worker.py"
                wrapper_environment = {
                    key: value for key, value in os.environ.items() if key in _INHERITED_ENVIRONMENT
                }
                wrapper_environment["PYTHONUNBUFFERED"] = "1"
                wrapper_environment.update(launch_config.provider_environment)
                process = subprocess.Popen(
                    [sys.executable, str(wrapper), str(launch_spec_path)],
                    cwd=instance_root,
                    env=wrapper_environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **wrapper_spawn_options(pinned_artifact.source_descriptor),
                )
                identity = process_start_identity(process.pid)
                if identity is None:
                    raise RuntimeError("the process start identity could not be read")
                started_at = utc_now()
                prepared.update(
                    {
                        "state": "STARTING",
                        "pid": process.pid,
                        "start_identity": identity,
                        "started_at": started_at,
                    }
                )
                # Persist the wrapper identity immediately. The child handshake
                # is a later enrichment and can also be recovered after a
                # control-plane crash.
                atomic_write_json(record_path, prepared)
                self._children[launch_id] = process
                self._write_process_projection(prepared, "STARTING")
                self._append_process_event(task_id, "PROCESS_LAUNCHED", prepared)
                handshake_deadline = time.monotonic() + 1
                while not handshake_path.exists() and time.monotonic() < handshake_deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
                if handshake_path.exists():
                    prepared.update(read_json(handshake_path))
                atomic_write_json(record_path, prepared)
                if crash_hook:
                    crash_hook("after_process_record")
                deadline = time.monotonic() + supervisor.startup_timeout_seconds
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("process exited before readiness")
                    if self._probe(port, spec.health_path) and self._probe(
                        port, spec.readiness_path
                    ):
                        break
                    time.sleep(0.05)
                else:
                    raise TimeoutError("process readiness timed out")
                prepared["state"] = "RUNNING"
                prepared["ready_at"] = utc_now()
                atomic_write_json(record_path, prepared)
                self._write_process_projection(prepared, "RUNNING")
                if not preserve_business_state:
                    self._transition(task_id, instance_id, "RUNNING", f"{launch_id}-running")
                self._append_process_event(task_id, "PROCESS_READY", prepared)
                return deepcopy(prepared)
            except Exception as exc:
                if process is not None and process_start_identity(process.pid) is not None:
                    self._terminate_group(
                        process.pid, float(supervisor.shutdown_grace_seconds)
                    )
                if launch_spec_path is not None:
                    launch_spec_path.unlink(missing_ok=True)
                self.port_allocator.release(launch_id, port)
                prepared.update(
                    {
                        "state": "FAILED_TO_START",
                        "exit_code": process.poll() if process is not None else None,
                        "failed_at": utc_now(),
                        "failure_type": type(exc).__name__,
                    }
                )
                atomic_write_json(record_path, prepared)
                self._write_process_projection(prepared, "EXITED")
                target = "FAILED" if preserve_business_state else "FAILED_TO_START"
                self._transition(task_id, instance_id, target, f"{launch_id}-failed")
                self._append_process_event(task_id, "PROCESS_START_FAILED", prepared)
                if isinstance(exc, HarnessError):
                    raise
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The instance process did not become ready.",
                    {"failure_type": type(exc).__name__, "launch_id": launch_id},
                ) from None


    def _write_process_projection(self, record: dict[str, Any], state: str) -> None:
        process = None
        if record.get("pid"):
            process = {
                "pid": record["pid"],
                "port": record["port"],
                "launch_id": record["launch_id"],
                "state": state,
                "started_at": record["started_at"] or record["prepared_at"],
            }
        ui_url = (
            f"http://{self.host}:{record['port']}{record['ui_path']}"
            if state == "RUNNING"
            else None
        )
        self.store.update_instance_fields(
            record["task_id"],
            record["instance_id"],
            {"process": process, "ui_url": ui_url},
            actor=Actor("system", "process_supervisor"),
            command="update_process_projection",
            idempotency_key=f"process-{record['launch_id']}-{state.lower()}",
        )

    def _child_environment(
        self,
        task_id: str,
        instance_id: str,
        port: int,
        spec: ProcessSpec,
        launch_config: ImageAgentLaunchConfiguration,
    ) -> dict[str, str]:
        environment = {
            key: value for key, value in os.environ.items() if key in _INHERITED_ENVIRONMENT
        }
        environment.update(spec.public_environment)
        instance_root = self.store.layout.initialize_instance(task_id, instance_id)
        environment.update(
            {
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "HARNESS_TASK_ID": task_id,
                "HARNESS_INSTANCE_ID": instance_id,
                "HARNESS_INSTANCE_PORT": str(port),
                "IMAGE_AGENT_PROJECTS_ROOT": str(instance_root / "work"),
                "IMAGE_AGENT_RUNTIME_POLICY": str(launch_config.runtime_path),
                "IMAGE_AGENT_MODEL_CONFIG": str(launch_config.model_config_path),
                "HARNESS_CONFIG_REVISION": launch_config.source_config_revision,
            }
        )
        return environment

    def _enforce_code_identity(
        self, task_id: str, instance_id: str, code_identity: str
    ) -> None:
        identities = {
            item["code_identity"]
            for item in self._launches_for_instance(task_id, instance_id)
            if item.get("code_identity")
        }
        if identities and identities != {code_identity}:
            raise HarnessError(
                "PROCESS_START_FAILED",
                "The Agent code identity is fixed for the instance lifecycle.",
                {"instance_id": instance_id},
            )

    def _validate_start_eligibility(
        self, task_id: str, instance: dict[str, Any]
    ) -> None:
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "The instance stage and task are not authorized to start.",
                {"task_status": None, "instance_status": instance["status"]},
            )
        planned = any(
            item["instance_id"] == instance["instance_id"] for item in plan["instances"]
        )
        if (
            not planned
            or plan["task"]["status"] not in {"RUNNING", "FAILED"}
            or instance["status"] not in {"READY", "FAILED_TO_START", "FAILED", "CRASHED"}
            or instance["requirement_lifecycle"]["first_activated_at"] is None
        ):
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "The instance stage and task are not authorized to start.",
                {
                    "task_status": plan["task"]["status"],
                    "instance_status": instance["status"],
                },
            )

    def _record_is_alive(self, record: dict[str, Any]) -> bool:
        return bool(
            record.get("pid")
            and process_start_identity(record["pid"]) == record.get("start_identity")
        )

    def _hydrate_launch_identity(self, record: dict[str, Any]) -> dict[str, Any]:
        """Adopt a wrapper that started before its launch record gained a PID."""

        if record.get("pid") or record["state"] != "PREPARED":
            return record
        handshake_path = (
            self.store.layout.workspace_root
            / "tasks"
            / record["task_id"]
            / "instances"
            / record["instance_id"]
            / "runtime"
            / f"{record['launch_id']}.child.json"
        )
        if not handshake_path.exists() or handshake_path.is_symlink():
            return record
        wrapper_pid = child_pid = 0
        wrapper_identity = child_identity = ""
        try:
            handshake = read_json(handshake_path)
            wrapper_pid = int(handshake["wrapper_pid"])
            child_pid = int(handshake["child_pid"])
            wrapper_identity = handshake["wrapper_start_identity"]
            child_identity = handshake["child_start_identity"]
            valid = (
                wrapper_pid > 1
                and child_pid > 1
                and process_start_identity(wrapper_pid) == wrapper_identity
                and process_start_identity(child_pid) == child_identity
                and process_group_contains(wrapper_pid, child_pid)
            )
        except (KeyError, OSError, TypeError, ValueError):
            valid = False
        if not valid:
            return record
        adopted = {
            **record,
            "state": "STARTING",
            "pid": wrapper_pid,
            "start_identity": wrapper_identity,
            "child_pid": child_pid,
            "child_start_identity": child_identity,
            "started_at": record["prepared_at"],
        }
        atomic_write_json(self._launch_path(record["launch_id"]), adopted)
        self._append_process_event(record["task_id"], "PROCESS_ADOPTED", adopted)
        return adopted

    @staticmethod
    def _record_child_is_alive(record: dict[str, Any]) -> bool:
        child_pid = record.get("child_pid")
        if not child_pid:
            return False
        return bool(
            process_start_identity(child_pid) == record.get("child_start_identity")
            and process_group_contains(record["pid"], child_pid)
        )

    @staticmethod
    def _terminate_group(pid: int, grace_seconds: float) -> None:
        terminate_process_tree(pid, grace_seconds)

    def _probe(self, port: int, path: str) -> bool:
        connection = http.client.HTTPConnection(self.host, port, timeout=0.25)
        try:
            connection.request("GET", path, headers={"Connection": "close"})
            response = connection.getresponse()
            response.read(4096)
            return response.status == 200
        except (http.client.HTTPException, TimeoutError, OSError):
            return False
        finally:
            connection.close()

    def _promote_ready(self, record: dict[str, Any]) -> dict[str, Any]:
        record = deepcopy(record)
        record["state"] = "RUNNING"
        record["ready_at"] = utc_now()
        record["health_failures"] = 0
        atomic_write_json(self._launch_path(record["launch_id"]), record)
        self._write_process_projection(record, "RUNNING")
        instance = self._instance(record["task_id"], record["instance_id"])
        if instance["status"] == "STARTING":
            self._transition(
                record["task_id"],
                record["instance_id"],
                "RUNNING",
                f"{record['launch_id']}-running-recovered",
            )
        self._append_process_event(record["task_id"], "PROCESS_READY", record)
        return {"launch_id": record["launch_id"], "status": "RUNNING"}

    @staticmethod
    def _startup_elapsed(record: dict[str, Any]) -> float:
        value = record.get("started_at") or record["prepared_at"]
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - started).total_seconds()

    def _active_launch_for_instance(
        self, task_id: str, instance_id: str
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in reversed(self._launches_for_instance(task_id, instance_id))
                if item["state"] in ACTIVE_LAUNCH_STATES
            ),
            None,
        )

    def _launches_for_instance(
        self, task_id: str, instance_id: str
    ) -> list[dict[str, Any]]:
        values = []
        for path in sorted(self.launch_root.glob("*.json")):
            record = read_json(path)
            if record["task_id"] == task_id and record["instance_id"] == instance_id:
                values.append(record)
        return values

    def _all_instances(self) -> list[dict[str, Any]]:
        values = []
        root = self.store.layout.control_root / "tasks"
        for task_dir in sorted(root.iterdir() if root.exists() else []):
            if task_dir.is_dir():
                values.extend(self.store.instance.list(task_dir.name))
        return values

    def _instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        return instance

    def _interrupt_model_calls(self, task_id: str, instance_id: str, reason: str) -> None:
        root = self.store.layout.control_root / "tasks" / task_id / "attempts"
        if not root.exists():
            return
        for path in root.glob("*.json"):
            attempt_id = path.stem
            with FileLock(
                self._attempt_lock(task_id, attempt_id),
                self.store.lock_timeout_seconds,
            ):
                record = read_json(path)
                if (
                    record["instance_id"] != instance_id
                    or record["status"] != "IN_PROGRESS"
                ):
                    continue
                record.update(
                    {
                        "status": "INTERRUPTED",
                        "interrupted_at": utc_now(),
                        "reason": reason,
                    }
                )
                atomic_write_json(path, record)
                append_record(
                    self.store.layout.control_root / "tasks" / task_id / "events.ndjson",
                    {
                        "event_id": f"evt_{uuid.uuid4().hex}",
                        "event_type": "MODEL_CALL_INTERRUPTED",
                        "task_id": task_id,
                        "instance_id": instance_id,
                        "attempt_id": record["attempt_id"],
                        "request_id": record["request_id"],
                        "reason": reason,
                        "occurred_at": record["interrupted_at"],
                    },
                )

    def _attempt_path(self, task_id: str, attempt_id: str) -> Path:
        root = self.store.layout.control_root / "tasks" / task_id / "attempts"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root / f"{attempt_id}.json"

    def _attempt_lock(self, task_id: str, attempt_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "locks"
            / f"attempt-{task_id}-{attempt_id}.lock"
        )

    def _instance_thread_lock(
        self, task_id: str, instance_id: str
    ) -> threading.RLock:
        key = (task_id, instance_id)
        with self._lock_registry_guard:
            return self._instance_locks.setdefault(key, threading.RLock())

    def _append_process_event(
        self, task_id: str, event_type: str, record: dict[str, Any]
    ) -> None:
        append_record(
            self.store.layout.control_root / "tasks" / task_id / "events.ndjson",
            {
                "event_id": f"evt_{uuid.uuid4().hex}",
                "event_type": event_type,
                "task_id": task_id,
                "instance_id": record["instance_id"],
                "launch_id": record["launch_id"],
                "pid": record.get("pid"),
                "port": record["port"],
                "state": record["state"],
                "exit_code": record.get("exit_code"),
                "occurred_at": utc_now(),
            },
        )

    def _launch_path(self, launch_id: str) -> Path:
        return self.launch_root / f"{launch_id}.json"
