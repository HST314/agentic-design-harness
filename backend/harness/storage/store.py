"""File State Store composition, indexes and startup recovery."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..contracts import ContractRegistry
from ..core.errors import HarnessError
from .atomic import atomic_write_json, fsync_directory, read_json
from .idempotency import IdempotencyRepository
from .layout import StateLayout
from .locks import FileLock
from .ndjson import append_record, recover_records
from .repository import (
    Actor,
    ApprovalRepository,
    InboxRepository,
    InstanceRepository,
    MasterMessageRepository,
    MasterThreadRepository,
    PlanProposalRepository,
    PlanRepository,
    RetryBudgetRepository,
    StageRepository,
    TaskIntakeRepository,
    TaskNavigationRepository,
    TaskRepository,
    UsageRepository,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class FileStateStore:
    """The only state-writing service in one Harness control-plane process."""

    def __init__(
        self,
        control_root: Path,
        workspace_root: Path,
        contracts: ContractRegistry,
        lock_timeout_seconds: float = 5.0,
    ) -> None:
        self.layout = StateLayout(control_root, workspace_root)
        self.layout.initialize()
        self.contracts = contracts
        self.lock_timeout_seconds = lock_timeout_seconds
        self.writer_lease = FileLock(control_root / "locks" / "writer.lock", lock_timeout_seconds)
        self.task = TaskRepository(
            self.layout,
            "task",
            lambda _: Path("task.json"),
            lambda value: contracts.validate("main-task", value),
            lock_timeout_seconds,
            self.rebuild_task_index,
        )
        self.plan = PlanRepository(
            self.layout,
            "plan",
            lambda _: Path("plan.json"),
            lambda value: contracts.validate("task-plan", value),
            lock_timeout_seconds,
        )
        self.stage = StageRepository(
            self.layout,
            "stage",
            lambda object_id: Path("stages") / f"{object_id}.json",
            lambda value: contracts.validate("stage", value),
            lock_timeout_seconds,
        )
        self.instance = InstanceRepository(
            self.layout,
            "instance",
            lambda object_id: Path("instances") / f"{object_id}.json",
            lambda value: contracts.validate("agent-instance", value),
            lock_timeout_seconds,
        )
        self.approval = ApprovalRepository(
            self.layout,
            "approval",
            lambda object_id: Path("approvals") / f"{object_id}.json",
            lambda value: contracts.validate("approval-request", value),
            lock_timeout_seconds,
        )
        self.inbox = InboxRepository(
            self.layout,
            "inbox",
            lambda object_id: Path("inbox") / f"{object_id}.json",
            lambda value: contracts.validate("inbox-item", value),
            lock_timeout_seconds,
            self.rebuild_inbox_index,
        )
        self.retry_budget = RetryBudgetRepository(
            self.layout,
            "retry_budget",
            lambda _: Path("retry-budget.json"),
            self._validate_retry_budget,
            lock_timeout_seconds,
        )
        self.task_intake = TaskIntakeRepository(
            self.layout,
            "task_intake",
            lambda _: Path("task-intake.json"),
            lambda value: contracts.validate("task-intake", value),
            lock_timeout_seconds,
        )
        self.task_navigation = TaskNavigationRepository(
            self.layout,
            "task_navigation",
            lambda _: Path("task-navigation.json"),
            lambda value: contracts.validate("task-navigation-metadata", value),
            lock_timeout_seconds,
        )
        self.master_thread = MasterThreadRepository(
            self.layout,
            "master_thread",
            lambda _: Path("master") / "thread.json",
            lambda value: contracts.validate("master-thread", value),
            lock_timeout_seconds,
        )
        self.master_message = MasterMessageRepository(
            self.layout,
            "master_message",
            lambda object_id: Path("master") / "messages" / f"{object_id}.json",
            lambda value: contracts.validate("master-message", value),
            lock_timeout_seconds,
        )
        self.plan_proposal = PlanProposalRepository(
            self.layout,
            "plan_proposal",
            lambda object_id: Path("master") / "plan-proposals" / f"{object_id}.json",
            lambda value: contracts.validate("plan-proposal", value),
            lock_timeout_seconds,
        )
        self.usage = UsageRepository(
            self.layout,
            lambda value: contracts.validate("token-usage-event", value),
            lock_timeout_seconds,
        )
        self.idempotency = IdempotencyRepository(control_root / "idempotency", lock_timeout_seconds)
        self._repositories = {
            item.object_type: item
            for item in (
                self.task,
                self.plan,
                self.stage,
                self.instance,
                self.approval,
                self.inbox,
                self.retry_budget,
                self.task_intake,
                self.task_navigation,
                self.master_thread,
                self.master_message,
                self.plan_proposal,
            )
        }

    @staticmethod
    def _validate_retry_budget(value: dict[str, Any]) -> None:
        if "revision" not in value or not isinstance(value["revision"], int):
            raise ValueError("invalid retry budget snapshot")

    @property
    def ready(self) -> bool:
        return self.writer_lease.acquired and self.contracts.ready

    def start(self) -> list[dict[str, Any]]:
        self.writer_lease.acquire()
        try:
            return self.recover()
        except BaseException:
            self.writer_lease.release()
            raise

    def close(self) -> None:
        self.writer_lease.release()

    def _warning_sink(self, task_id: str):
        def write(warning: dict[str, Any]) -> None:
            append_record(
                self.layout.control_root / "recovery-warnings.ndjson",
                {**warning, "task_id": task_id, "occurred_at": _now()},
            )

        return write

    def recover(self) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        tasks_root = self.layout.control_root / "tasks"
        for task_directory in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
            if not task_directory.is_dir():
                continue
            task_id = task_directory.name

            def sink(warning: dict[str, Any], current_task_id: str = task_id) -> None:
                enriched = {
                    **warning,
                    "task_id": current_task_id,
                    "occurred_at": _now(),
                }
                warnings.append(enriched)
                append_record(self.layout.control_root / "recovery-warnings.ndjson", enriched)

            events = recover_records(task_directory / "events.ndjson", sink)
            recover_records(task_directory / "usage.ndjson", sink)
            self._recover_idempotency_results(task_id, events)
            latest: dict[tuple[str, str], dict[str, Any]] = {}
            for event in events:
                if event.get("event_type") != "OBJECT_COMMITTED":
                    continue
                key = (event["object_type"], event["object_id"])
                if key not in latest or event["revision"] > latest[key]["revision"]:
                    latest[key] = event
            for (object_type, object_id), event in latest.items():
                repository = self._repositories.get(object_type)
                if repository is None:
                    raise RuntimeError(f"unknown committed object type: {object_type}")
                path = repository.path(task_id, object_id)
                try:
                    current = read_json(path) if path.exists() else None
                except (OSError, ValueError):
                    current = None
                if current is None or int(current["revision"]) < int(event["revision"]):
                    atomic_write_json(path, event["snapshot"])
                elif int(current["revision"]) > int(event["revision"]):
                    raise RuntimeError(
                        f"uncommitted {object_type} snapshot revision for {object_id}"
                    )
                elif current != event["snapshot"]:
                    atomic_write_json(path, event["snapshot"])
                    sink(
                        {
                            "type": "SNAPSHOT_REBUILT",
                            "file": path.name,
                            "reason": "snapshot diverged from its committed event",
                        }
                    )
            snapshot_paths = [
                task_directory / "task.json",
                task_directory / "plan.json",
                task_directory / "retry-budget.json",
                task_directory / "task-intake.json",
                task_directory / "task-navigation.json",
                task_directory / "master" / "thread.json",
                *sorted((task_directory / "master" / "messages").glob("*.json")),
                *sorted((task_directory / "master" / "plan-proposals").glob("*.json")),
                *sorted((task_directory / "stages").glob("*.json")),
                *sorted((task_directory / "instances").glob("*.json")),
                *sorted((task_directory / "approvals").glob("*.json")),
                *sorted((task_directory / "inbox").glob("*.json")),
            ]
            for path in snapshot_paths:
                if not path.exists():
                    continue
                wrapper = read_json(path)
                key = (wrapper.get("object_type"), wrapper.get("object_id"))
                if key not in latest:
                    raise RuntimeError(f"snapshot without a commit event: {path.name}")
            self._reconcile_plan_projections(task_id)
        self.rebuild_task_index("")
        self.rebuild_inbox_index("")
        return warnings

    def lookup_committed_command_result(
        self,
        scope: str,
        key: str,
        command: str,
        request_sha256: str,
    ) -> dict[str, Any] | None:
        """Read the exact result carried by an authoritative business commit."""

        path = self.layout.control_root / "tasks" / scope / "events.ndjson"
        committed: dict[str, Any] | None = None
        for event in recover_records(path, self._warning_sink(scope)):
            if event.get("command_result") is None or event.get("idempotency_key") != key:
                continue
            if event.get("command") != command or event.get("request_sha256") != request_sha256:
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key was already used for a different request.",
                    {"scope": scope, "command": command},
                )
            committed = event["command_result"]
        return committed

    def _recover_idempotency_results(
        self, task_id: str, events: list[dict[str, Any]]
    ) -> None:
        for event in events:
            result = event.get("command_result")
            request_sha256 = event.get("request_sha256")
            if result is None or request_sha256 is None:
                continue
            self.idempotency.remember_digest(
                task_id,
                event["idempotency_key"],
                event["command"],
                request_sha256,
                result,
            )

    def _reconcile_plan_projections(self, task_id: str) -> None:
        """Complete an aggregate commit whose authoritative plan landed first."""

        plan_wrapper = self.plan.read_wrapper(task_id, task_id)
        if plan_wrapper is None:
            return
        plan = plan_wrapper["payload"]
        projections = [
            (self.task, task_id, plan["task"]),
            *((self.stage, item["stage_id"], item) for item in plan["stages"]),
            *((self.instance, item["instance_id"], item) for item in plan["instances"]),
        ]
        actor = Actor("system", "startup_recovery")
        for repository, object_id, payload in projections:
            current = repository.read_wrapper(task_id, object_id)
            if current is not None and current["payload"] == payload:
                continue
            repository.put(
                task_id,
                object_id,
                payload,
                expected_revision=0 if current is None else current["revision"],
                actor=actor,
                command="reconcile_plan_projection",
                idempotency_key=(
                    f"recover-{plan_wrapper['revision']}-{repository.object_type}-{object_id}"
                ),
            )
        self.retire_plan_projections(task_id, plan)
        workspace_task = self.layout.workspace_root / "tasks" / task_id
        atomic_write_json(workspace_task / "task-summary.json", plan["task"], mode=0o640)

    def retire_plan_projections(self, task_id: str, plan: dict[str, Any]) -> None:
        """Remove Stage/Instance projections absent from the authoritative plan."""

        projection_sets = (
            (self.stage, "stage_id", {item["stage_id"] for item in plan["stages"]}),
            (
                self.instance,
                "instance_id",
                {item["instance_id"] for item in plan["instances"]},
            ),
        )
        for repository, identifier, active_ids in projection_sets:
            for payload in repository.list(task_id):
                object_id = payload[identifier]
                if object_id in active_ids:
                    continue
                path = repository.path(task_id, object_id)
                path.unlink(missing_ok=True)
                fsync_directory(path.parent)

    def update_instance_fields(
        self,
        task_id: str,
        instance_id: str,
        changes: dict[str, Any],
        *,
        actor: Actor,
        command: str,
        idempotency_key: str,
        crash_hook=None,
    ) -> dict[str, Any]:
        """Commit mutable instance facts through the authoritative plan when present.

        The plan is the recovery anchor for planned instances. A committed plan event
        is therefore written before its instance projection. Instance records created
        before plan construction are updated directly.
        """

        allowed = {
            "config_revision",
            "credential_pair_ref",
            "credential_pair_revision",
            "process",
            "restart_required",
            "ui_url",
        }
        if not changes or not set(changes).issubset(allowed):
            raise ValueError("unsupported mutable instance fields")
        lock = FileLock(
            self.layout.control_root / "locks" / f"command-{task_id}.lock",
            self.lock_timeout_seconds,
        )
        with lock:
            plan = self.plan.get(task_id, task_id)
            if plan is not None:
                updated_plan = deepcopy(plan)
                instance = next(
                    (
                        item
                        for item in updated_plan["instances"]
                        if item["instance_id"] == instance_id
                    ),
                    None,
                )
                if instance is not None:
                    if all(instance.get(key) == value for key, value in changes.items()):
                        return deepcopy(instance)
                    instance.update(deepcopy(changes))
                    self.contracts.validate("task-plan", updated_plan)
                    self.plan.put(
                        task_id,
                        task_id,
                        updated_plan,
                        expected_revision=self.plan.revision(task_id, task_id),
                        actor=actor,
                        command=command,
                        idempotency_key=idempotency_key,
                        crash_hook=crash_hook,
                    )
                    self._reconcile_plan_projections(task_id)
                    return deepcopy(instance)
            current = self.instance.get(task_id, instance_id)
            if current is None:
                raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
            if all(current.get(key) == value for key, value in changes.items()):
                return deepcopy(current)
            updated = {**deepcopy(current), **deepcopy(changes)}
            self.instance.put(
                task_id,
                instance_id,
                updated,
                expected_revision=self.instance.revision(task_id, instance_id),
                actor=actor,
                command=command,
                idempotency_key=idempotency_key,
                crash_hook=crash_hook,
            )
            return updated

    def rebuild_task_index(self, _: str = "") -> None:
        tasks: list[dict[str, Any]] = []
        root = self.layout.control_root / "tasks"
        for path in sorted(root.glob("*/task.json")):
            wrapper = read_json(path)
            payload = wrapper["payload"]
            tasks.append(
                {
                    "task_id": payload["task_id"],
                    "status": payload["status"],
                    "title": payload["title"],
                    "updated_at": payload["updated_at"],
                    "revision": wrapper["revision"],
                }
            )
        atomic_write_json(
            self.layout.control_root / "indexes" / "task-index.json",
            {"rebuilt_at": _now(), "tasks": tasks},
        )

    def rebuild_inbox_index(self, _: str = "") -> None:
        entries: list[dict[str, Any]] = []
        root = self.layout.control_root / "tasks"
        for path in sorted(root.glob("*/inbox/*.json")):
            wrapper = read_json(path)
            payload = wrapper["payload"]
            entries.append(
                {
                    "inbox_id": payload["inbox_id"],
                    "task_id": payload["task_id"],
                    "kind": payload["kind"],
                    "owner": payload["owner"],
                    "status": payload["status"],
                    "created_at": payload["created_at"],
                    "sequence": payload["sequence"],
                }
            )
        entries.sort(key=lambda item: (item["created_at"], item["sequence"], item["inbox_id"]))
        atomic_write_json(
            self.layout.control_root / "indexes" / "inbox-index.json",
            {"rebuilt_at": _now(), "entries": entries},
        )
