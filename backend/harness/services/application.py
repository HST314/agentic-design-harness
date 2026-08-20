"""Recoverable application use cases above domain and infrastructure services."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..adapters import AdapterRegistry, PrepareRequest
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..domain.service import TaskCommandService
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .assets import AssetService
from .credentials import CredentialPoolService
from .supervisor import ProcessSupervisor

CrashHook = Callable[[str], None]


class HarnessApplicationService:
    """Own multi-service workflows so API and Master calls cannot reorder them."""

    def __init__(
        self,
        store: FileStateStore,
        commands: TaskCommandService,
        assets: AssetService,
        credentials: CredentialPoolService,
        supervisor: ProcessSupervisor,
        adapters: AdapterRegistry,
    ) -> None:
        self.store = store
        self.commands = commands
        self.assets = assets
        self.credentials = credentials
        self.supervisor = supervisor
        self.adapters = adapters
        self.intent_root = store.layout.control_root / "application-intents"
        self.intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save_plan_and_create_instances(
        self,
        task_id: str,
        *,
        stages: list[dict[str, Any]],
        instances: list[dict[str, Any]],
        task_cards: list[dict[str, Any]],
        providers: dict[str, str],
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(operation_id, "operation_id")
        request = {
            "task_id": task_id,
            "stages": deepcopy(stages),
            "instances": deepcopy(instances),
            "task_cards": deepcopy(task_cards),
            "providers": dict(sorted(providers.items())),
            "envelope": envelope.model_dump(mode="json"),
        }
        request_sha256 = digest_json(request)
        intent_path = self._intent_path(operation_id)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            if intent_path.exists():
                intent = read_json(intent_path)
                if intent.get("request_sha256") != request_sha256:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The application operation id was reused for another request.",
                        {"operation_id": operation_id},
                    )
                if intent["state"] == "COMMITTED":
                    return deepcopy(intent["result"])
                if intent["state"] == "ABORTED":
                    self._raise_terminal_intent(intent)
            else:
                self._prevalidate_plan(request)
                prepared_at = utc_now()
                intent = {
                    "schema_version": "1.0",
                    "kind": "SAVE_PLAN_AND_CREATE_INSTANCES",
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "creation_instances": {
                        item["instance_id"]: self._creation_summary(
                            task_id, item, prepared_at
                        )
                        for item in request["instances"]
                        if item["instance_id"] in providers
                    },
                    "state": "PREPARED",
                    "prepared_at": prepared_at,
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_application_intent")
            return self._resume_save_plan(intent_path, crash_hook)

    def recover(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.intent_root.glob("*.json")):
            operation_id = path.stem
            task_id = read_json(path)["request"]["task_id"]
            with (
                FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
                FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
            ):
                intent = read_json(path)
                if intent["state"] in {"COMMITTED", "ABORTED"}:
                    continue
                try:
                    if intent["kind"] == "SAVE_PLAN_AND_CREATE_INSTANCES":
                        with self.commands.task_guard(task_id):
                            result = self._resume_save_plan(path, None)
                    elif intent["kind"] == "START_READY_INSTANCES":
                        result = self._resume_start(path, None)
                    else:
                        raise HarnessError(
                            "VALIDATION_ERROR", "The application intent kind is invalid."
                        )
                    results.append(
                        {"operation_id": operation_id, "status": "RECOVERED", "result": result}
                    )
                except HarnessError as exc:
                    state = read_json(path)["state"]
                    results.append(
                        {
                            "operation_id": operation_id,
                            "status": "ABORTED" if state == "ABORTED" else "PENDING",
                            "error_code": exc.code,
                        }
                    )
        return results

    def confirm_and_start_ready_instances(
        self,
        task_id: str,
        *,
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(operation_id, "operation_id")
        request = {
            "task_id": task_id,
            "envelope": envelope.model_dump(mode="json"),
        }
        request_sha256 = digest_json(request)
        intent_path = self._intent_path(operation_id)
        with FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds), FileLock(
            self._intent_lock(operation_id), self.store.lock_timeout_seconds
        ):
            if intent_path.exists():
                intent = read_json(intent_path)
                if (
                    intent.get("kind") != "START_READY_INSTANCES"
                    or intent.get("request_sha256") != request_sha256
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The application operation id was reused for another request.",
                        {"operation_id": operation_id},
                    )
                if intent["state"] == "COMMITTED":
                    return deepcopy(intent["result"])
            else:
                plan = self._plan(task_id)
                if plan["task"]["status"] not in {
                    "AWAITING_START_CONFIRMATION",
                    "RUNNING",
                    "BLOCKED_UNAVAILABLE",
                }:
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION",
                        "Only a planned task may start ready Agent instances.",
                        {"current": plan["task"]["status"]},
                    )
                targets = [
                    item["instance_id"]
                    for item in plan["instances"]
                    if item["status"] == "READY"
                ]
                unavailable = [
                    item["instance_id"]
                    for item in plan["instances"]
                    if item["status"] == "UNAVAILABLE"
                ]
                intent = {
                    "schema_version": "1.0",
                    "kind": "START_READY_INSTANCES",
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "target_instance_ids": targets,
                    "unavailable": unavailable,
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_start_intent")
            return self._resume_start(intent_path, crash_hook)

    def cancel_instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        if instance["status"] == "CANCELLED":
            return instance
        return self.supervisor.cancel_instance(task_id, instance_id)

    def archive_instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        if instance["status"] == "ARCHIVED":
            return instance
        return self.supervisor.archive_instance(task_id, instance_id)

    def publish_delivery_and_complete(
        self,
        task_id: str,
        instance_id: str,
        *,
        source_relative_path: str,
        role: str,
        description: str,
        operation_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        manifest = self.assets.publish_delivery(
            task_id,
            instance_id,
            source_relative_path=source_relative_path,
            role=role,
            description=description,
            idempotency_key=operation_id,
        )
        plan = self._plan(task_id)
        card = next(
            item for item in plan["task_cards"] if item["instance_id"] == instance_id
        )
        published = [
            self.assets.verify_asset(task_id, item["manifest"]["asset_id"])
            for item in self.assets.list_assets(task_id)
            if item["integrity_status"] == "VERIFIED"
            and item["manifest"].get("producer_instance_id") == instance_id
        ]
        complete = all(
            any(
                candidate["role"] == expected["role"]
                and candidate["kind"] == expected["kind"]
                and candidate["mime_type"] in expected["accepted_mime_types"]
                for candidate in published
            )
            for expected in card["expected_deliveries"]
            if expected["required"]
        )
        transition = None
        if complete:
            transition = self.commands.transition_instance(
                task_id, instance_id, "SUCCEEDED", envelope
            )
        return {"manifest": manifest, "complete": complete, "transition": transition}

    def _resume_save_plan(
        self, intent_path: Path, crash_hook: CrashHook | None
    ) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        actor = Actor(
            request["envelope"]["actor_type"], request["envelope"]["actor_id"]
        )
        assigned_instances: list[dict[str, Any]] = []
        for raw in request["instances"]:
            instance = deepcopy(raw)
            provider = request["providers"].get(raw["instance_id"])
            if provider is not None:
                try:
                    self.commands.validate_task_revision(
                        request["task_id"], request["envelope"]["expected_revision"]
                    )
                except HarnessError as exc:
                    if exc.code == "REVISION_CONFLICT":
                        self._abort_stale_save_plan(intent_path, intent, actor, exc)
                    raise
                created = self.credentials.create_instance(
                    request["task_id"],
                    intent["creation_instances"][raw["instance_id"]],
                    provider=provider,
                    creation_id=self._derived_id(
                        "creation", intent["operation_id"], raw["instance_id"]
                    ),
                    actor=actor,
                )
                credential = created["credential"]
                instance.update(
                    {
                        "credential_pair_ref": credential["credential_pair_id"],
                        "credential_pair_revision": credential[
                            "credential_pair_revision"
                        ],
                    }
                )
                if crash_hook:
                    crash_hook(f"after_instance_created:{raw['instance_id']}")
            else:
                adapter = self.adapters.get_optional(raw["agent_type"])
                if adapter is not None and not adapter.available:
                    instance.update(
                        {
                            "credential_pair_ref": f"{raw['agent_type']}_adapter_unavailable",
                            "credential_pair_revision": 1,
                        }
                    )
            assigned_instances.append(instance)
        result = self.commands.save_plan(
            request["task_id"],
            stages=request["stages"],
            instances=assigned_instances,
            task_cards=request["task_cards"],
            envelope=CommandEnvelope.model_validate(request["envelope"]),
        )
        if crash_hook:
            crash_hook("after_plan_commit")
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return deepcopy(result)

    def _abort_stale_save_plan(
        self,
        intent_path: Path,
        intent: dict[str, Any],
        actor: Actor,
        error: HarnessError,
    ) -> None:
        intent.update({"state": "COMPENSATING", "compensation_started_at": utc_now()})
        atomic_write_json(intent_path, intent)
        compensation = []
        request = intent["request"]
        for instance_id in sorted(request["providers"]):
            creation_id = self._derived_id("creation", intent["operation_id"], instance_id)
            compensation.append(
                self.credentials.revoke_instance_creation(
                    request["task_id"],
                    creation_id,
                    revocation_id=self._derived_id(
                        "revoke", intent["operation_id"], instance_id
                    ),
                    actor=actor,
                )
            )
        intent.update(
            {
                "state": "ABORTED",
                "aborted_at": utc_now(),
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": deepcopy(error.details),
                },
                "compensation": compensation,
            }
        )
        atomic_write_json(intent_path, intent)

    @staticmethod
    def _raise_terminal_intent(intent: dict[str, Any]) -> None:
        error = intent["error"]
        raise HarnessError(error["code"], error["message"], deepcopy(error["details"]))

    def _resume_start(
        self, intent_path: Path, crash_hook: CrashHook | None
    ) -> dict[str, Any]:
        intent = read_json(intent_path)
        task_id = intent["request"]["task_id"]
        plan = self._plan(task_id)
        if plan["task"]["status"] == "AWAITING_START_CONFIRMATION":
            plan = self.commands.confirm_start(
                task_id,
                CommandEnvelope.model_validate(intent["request"]["envelope"]),
            )["plan"]
        elif plan["task"]["status"] not in {"RUNNING", "BLOCKED_UNAVAILABLE"}:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A prepared start intent no longer belongs to an active task.",
                {"current": plan["task"]["status"]},
            )
        task_root = self.store.layout.workspace_root / "tasks" / task_id
        instances = {item["instance_id"]: item for item in plan["instances"]}
        cards = {item["instance_id"]: item for item in plan["task_cards"]}
        launches: list[dict[str, Any]] = []
        for instance_id in intent["target_instance_ids"]:
            instance = instances.get(instance_id)
            if instance is None or instance["status"] in {
                "CANCELLED",
                "SUPERSEDED",
                "ARCHIVED",
            }:
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "A prepared start intent no longer owns a startable instance.",
                    {"instance_id": instance_id},
                )
            adapter = self.adapters.get(instance["agent_type"])
            if not adapter.available:
                raise HarnessError(
                    "ADAPTER_UNAVAILABLE",
                    "A prepared start intent references an unavailable adapter.",
                    {"instance_id": instance_id},
                )
            self._require_valid_card(adapter, cards[instance_id])
            spec = adapter.prepare(
                PrepareRequest(
                    instance=deepcopy(instance),
                    task_card=deepcopy(cards[instance_id]),
                    task_root=task_root,
                    config_ref=task_root
                    / "instances"
                    / instance_id
                    / "runtime"
                    / "runtime.yaml",
                    credential_ref=(
                        instance["credential_pair_ref"],
                        instance["credential_pair_revision"],
                    ),
                )
            )
            launch_id = self._derived_id("launch", intent["operation_id"], instance_id)
            attempt_id = self._derived_id("attempt", intent["operation_id"], instance_id)
            launch = self.supervisor.start_instance(
                task_id,
                instance_id,
                spec,
                launch_id=launch_id,
                attempt_id=attempt_id,
            )
            if launch["state"] != "RUNNING":
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "A prepared start intent cannot reuse a non-running launch.",
                    {"instance_id": instance_id, "launch_state": launch["state"]},
                )
            if crash_hook:
                crash_hook(f"after_process_started:{instance_id}")
            adapter_result = adapter.start(instance_id, attempt_id)
            if not adapter_result.accepted:
                raise HarnessError(
                    "PROCESS_START_FAILED",
                    "The Agent adapter rejected the prepared start operation.",
                    {"instance_id": instance_id},
                )
            launches.append(
                {
                    "instance_id": instance_id,
                    "launch": self._launch_summary(launch),
                    "adapter": {
                        "accepted": adapter_result.accepted,
                        "operation_id": adapter_result.operation_id,
                        "details": adapter_result.details,
                    },
                }
            )
        result = {
            "task_id": task_id,
            "launches": launches,
            "unavailable": intent["unavailable"],
        }
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return deepcopy(result)

    def _prevalidate_plan(self, request: dict[str, Any]) -> None:
        instance_ids = {item["instance_id"] for item in request["instances"]}
        unknown_providers = set(request["providers"]) - instance_ids
        if unknown_providers:
            raise HarnessError(
                "VALIDATION_ERROR",
                "A Provider mapping references an unknown instance.",
                {"instance_ids": sorted(unknown_providers)},
            )
        for instance in request["instances"]:
            adapter = self.adapters.get_optional(instance["agent_type"])
            has_provider = instance["instance_id"] in request["providers"]
            if (adapter is None or adapter.available) and not has_provider:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A runnable Agent instance requires an explicit Provider mapping.",
                    {"instance_id": instance["instance_id"]},
                )
            if adapter is not None and not adapter.available and has_provider:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "An unavailable Agent placeholder cannot consume a credential pair.",
                    {"instance_id": instance["instance_id"]},
                )
        provisional = []
        for raw in request["instances"]:
            instance = deepcopy(raw)
            if raw["instance_id"] in request["providers"]:
                instance.setdefault("credential_pair_ref", "pending_assignment")
                instance.setdefault("credential_pair_revision", 1)
            provisional.append(instance)
        self.commands.validate_plan_request(
            request["task_id"],
            stages=request["stages"],
            instances=provisional,
            task_cards=request["task_cards"],
            expected_revision=request["envelope"]["expected_revision"],
        )
        for card in request["task_cards"]:
            adapter = self.adapters.get_optional(card["agent_type"])
            if adapter is not None:
                self._require_valid_card(adapter, card)

    @staticmethod
    def _require_valid_card(adapter, card: dict[str, Any]) -> None:
        validation = adapter.validate_task_card(card)
        if not validation.valid:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Agent adapter rejected its task card.",
                {"errors": list(validation.errors)},
            )

    @staticmethod
    def _creation_summary(
        task_id: str, raw: dict[str, Any], created_at: str
    ) -> dict[str, Any]:
        required = bool(raw["required"])
        return {
            **deepcopy(raw),
            "schema_version": "1.0",
            "task_id": task_id,
            "requirement_lifecycle": {
                "original_required": required,
                "first_activated_at": None,
                "authorized_downgrade": None,
            },
            "status": "CREATED",
            "process": None,
            "ui_url": None,
            "created_at": created_at,
        }

    def _plan(self, task_id: str) -> dict[str, Any]:
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            raise HarnessError("TASK_NOT_FOUND", "The task does not have a saved plan.")
        return deepcopy(plan)

    def _instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        return deepcopy(instance)

    def _intent_path(self, operation_id: str) -> Path:
        return self.intent_root / f"{operation_id}.json"

    def _intent_lock(self, operation_id: str) -> Path:
        return self.store.layout.control_root / "locks" / f"application-{operation_id}.lock"

    def _task_lock(self, task_id: str) -> Path:
        return self.store.layout.control_root / "locks" / f"application-task-{task_id}.lock"

    @staticmethod
    def _derived_id(prefix: str, operation_id: str, instance_id: str) -> str:
        digest = hashlib.sha256(f"{operation_id}:{instance_id}".encode()).hexdigest()
        return f"{prefix}_{digest[:24]}"

    @staticmethod
    def _launch_summary(launch: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "task_id",
            "instance_id",
            "launch_id",
            "attempt_id",
            "state",
            "host",
            "port",
            "pid",
            "started_at",
            "code_identity",
        )
        return {name: launch.get(name) for name in fields}
