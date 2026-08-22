"""Frozen-owner approvals and durable FIFO inbox notifications."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any, Literal, cast

from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.paths import resolve_task_path
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore


class ApprovalInboxService:
    """Create approvals once, freeze their owner, and reduce notifications."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store
        self.sequence_path = store.layout.control_root / "indexes" / "activity-sequence.json"
        self.sequence_lock = store.layout.control_root / "locks" / "activity-sequence.lock"

    def ensure_workflow_approval(
        self,
        task_id: str,
        instance_id: str,
        *,
        step_id: str,
        capabilities: list[str],
        context: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        validate_identifier(step_id, "step_id")
        if not capabilities or any(not isinstance(item, str) for item in capabilities):
            raise HarnessError(
                "VALIDATION_ERROR", "A workflow approval requires at least one legal action."
            )
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance["task_id"] != task_id:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        identity = digest_json(
            {
                "task_id": task_id,
                "instance_id": instance_id,
                "step_id": step_id,
                "operation_id": operation_id,
            }
        )
        approval_id = f"ap_{identity[:24]}"
        approval_lock = self.store.layout.control_root / "locks" / f"approval-{task_id}.lock"
        with FileLock(approval_lock, self.store.lock_timeout_seconds):
            existing = self.store.approval.get(task_id, approval_id)
            if existing is None:
                now = utc_now()
                payload_ref = f"approvals/{approval_id}/request.json"
                workspace = self.store.layout.initialize_task(task_id)[1]
                approvals_root = resolve_task_path(
                    workspace,
                    "approvals",
                    allowed_prefixes=("approvals",),
                )
                approval_dir = approvals_root / approval_id
                approval_dir.mkdir(exist_ok=True, mode=0o700)
                payload_path = approval_dir / "request.json"
                atomic_write_json(
                    payload_path,
                    {
                        "schema_version": "1.0",
                        "approval_id": approval_id,
                        "task_id": task_id,
                        "instance_id": instance_id,
                        "step_id": step_id,
                        "available_actions": sorted(set(capabilities)),
                        "context": deepcopy(context),
                        "operation_id": operation_id,
                        "created_at": now,
                    },
                    mode=0o640,
                )
                existing = {
                    "schema_version": "1.0",
                    "approval_id": approval_id,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "step_id": step_id,
                    "kind": "WORKFLOW",
                    "owner": instance["approval_mode"],
                    "status": "PENDING",
                    "payload_ref": payload_ref,
                    "created_at": now,
                    "sequence": self._next_sequence(),
                    "revision": 1,
                }
                self.store.approval.put(
                    task_id,
                    approval_id,
                    existing,
                    expected_revision=0,
                    actor=Actor("adapter", f"{instance['agent_type']}_adapter"),
                    command="ensure_workflow_approval",
                    idempotency_key=f"ensure-{approval_id}",
                )
        notification = self.ensure_notification(
            task_id,
            kind="APPROVAL_REQUIRED",
            owner=existing["owner"],
            title="工作流等待决议",
            message=f"实例 {instance_id} 正在 {step_id} 等待处理。",
            deep_link=f"inbox?approval_id={approval_id}",
            dedupe_key=f"approval:{approval_id}",
            instance_id=instance_id,
            approval_id=approval_id,
        )
        return {
            "approval": deepcopy(existing),
            "approval_revision": self.store.approval.revision(task_id, approval_id),
            "notification": notification,
        }

    def ensure_delivery_review_approval(
        self,
        task_id: str,
        instance_id: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        """Create the human-only decision gate for one immutable branch bundle."""

        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance.get("task_id") != task_id:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        self.store.contracts.validate("delivery-bundle-candidate", candidate)
        if (
            candidate["task_id"] != task_id
            or candidate["instance_id"] != instance_id
            or candidate["status"] != "PENDING_CONFIRMATION"
        ):
            raise HarnessError(
                "VALIDATION_ERROR", "The delivery review candidate has an invalid owner or state."
            )
        bundle_id = candidate["bundle_id"]
        approval_id = f"ap_{digest_json({'kind': 'DELIVERY_REVIEW', 'bundle_id': bundle_id})[:24]}"
        step_id = f"delivery_{digest_json(bundle_id)[:24]}"
        approval_lock = self.store.layout.control_root / "locks" / f"approval-{task_id}.lock"
        with FileLock(approval_lock, self.store.lock_timeout_seconds):
            existing = self.store.approval.get(task_id, approval_id)
            if existing is None:
                now = utc_now()
                payload_ref = f"approvals/{approval_id}/request.json"
                workspace = self.store.layout.initialize_task(task_id)[1]
                approvals_root = resolve_task_path(
                    workspace,
                    "approvals",
                    allowed_prefixes=("approvals",),
                )
                approval_dir = approvals_root / approval_id
                approval_dir.mkdir(exist_ok=True, mode=0o700)
                atomic_write_json(
                    approval_dir / "request.json",
                    {
                        "schema_version": "1.0",
                        "approval_id": approval_id,
                        "task_id": task_id,
                        "instance_id": instance_id,
                        "step_id": step_id,
                        "kind": "DELIVERY_REVIEW",
                        "bundle_id": bundle_id,
                        "available_actions": ["publish_bundle"],
                        "context": {"candidate": deepcopy(candidate)},
                        "created_at": now,
                    },
                    mode=0o640,
                )
                existing = {
                    "schema_version": "1.0",
                    "approval_id": approval_id,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "step_id": step_id,
                    "kind": "DELIVERY_REVIEW",
                    "owner": "human",
                    "status": "PENDING",
                    "payload_ref": payload_ref,
                    "created_at": now,
                    "sequence": self._next_sequence(),
                    "revision": 1,
                }
                self.store.approval.put(
                    task_id,
                    approval_id,
                    existing,
                    expected_revision=0,
                    actor=Actor("adapter", f"{instance['agent_type']}_adapter"),
                    command="ensure_delivery_review_approval",
                    idempotency_key=f"ensure-{approval_id}",
                )
            elif existing["kind"] != "DELIVERY_REVIEW":
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The deterministic delivery approval id belongs to another request.",
                )
        notification = self.ensure_notification(
            task_id,
            kind="DELIVERY_REVIEW_REQUIRED",
            owner="human",
            title="分支交付包等待确认",
            message=f"实例 {instance_id} 的分支 {candidate['branch_id']} 已冻结图片与设计说明。",
            deep_link=f"tasks/{task_id}/deliveries?bundle_id={bundle_id}",
            dedupe_key=f"delivery-review:{bundle_id}",
            instance_id=instance_id,
            approval_id=approval_id,
        )
        return {
            "approval": deepcopy(existing),
            "approval_revision": self.store.approval.revision(task_id, approval_id),
            "notification": notification,
        }

    def ensure_budget_approval(
        self,
        task_id: str,
        instance_id: str,
        *,
        attempt_id: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Create the human-only, deterministic override request for one retry."""

        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        validate_identifier(attempt_id, "attempt_id")
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance.get("task_id") != task_id:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        approval_id, step_id = self.budget_approval_identity(
            task_id, instance_id, attempt_id
        )
        approval_lock = self.store.layout.control_root / "locks" / f"approval-{task_id}.lock"
        with FileLock(approval_lock, self.store.lock_timeout_seconds):
            existing = self.store.approval.get(task_id, approval_id)
            if existing is None:
                now = utc_now()
                payload_ref = f"approvals/{approval_id}/request.json"
                workspace = self.store.layout.initialize_task(task_id)[1]
                approvals_root = resolve_task_path(
                    workspace,
                    "approvals",
                    allowed_prefixes=("approvals",),
                )
                approval_dir = approvals_root / approval_id
                approval_dir.mkdir(exist_ok=True, mode=0o700)
                atomic_write_json(
                    approval_dir / "request.json",
                    {
                        "schema_version": "1.0",
                        "approval_id": approval_id,
                        "task_id": task_id,
                        "instance_id": instance_id,
                        "step_id": step_id,
                        "attempt_id": attempt_id,
                        "available_actions": ["approve_once"],
                        "context": deepcopy(context),
                        "created_at": now,
                    },
                    mode=0o640,
                )
                existing = {
                    "schema_version": "1.0",
                    "approval_id": approval_id,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "step_id": step_id,
                    "kind": "BUDGET_OVERRIDE",
                    "owner": "human",
                    "status": "PENDING",
                    "payload_ref": payload_ref,
                    "created_at": now,
                    "sequence": self._next_sequence(),
                    "revision": 1,
                }
                self.store.approval.put(
                    task_id,
                    approval_id,
                    existing,
                    expected_revision=0,
                    actor=Actor("system", "retry_budget_gate"),
                    command="ensure_budget_approval",
                    idempotency_key=f"ensure-{approval_id}",
                )
            elif existing["kind"] != "BUDGET_OVERRIDE":
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The deterministic budget approval id belongs to another request.",
                )
        notification = self.ensure_notification(
            task_id,
            kind="BUDGET_APPROVAL_REQUIRED",
            owner="human",
            title="自动重试需要预算确认",
            message=f"实例 {instance_id} 的重试 {attempt_id} 被预算闸门阻断。",
            deep_link=f"inbox?approval_id={approval_id}",
            dedupe_key=f"budget-approval:{approval_id}",
            instance_id=instance_id,
            approval_id=approval_id,
        )
        return {
            "approval": deepcopy(existing),
            "approval_revision": self.store.approval.revision(task_id, approval_id),
            "notification": notification,
        }

    @staticmethod
    def budget_approval_identity(
        task_id: str, instance_id: str, attempt_id: str
    ) -> tuple[str, str]:
        identity = digest_json(
            {
                "kind": "BUDGET_OVERRIDE",
                "task_id": task_id,
                "instance_id": instance_id,
                "attempt_id": attempt_id,
            }
        )
        return f"ap_{identity[:24]}", f"budget_{identity[:24]}"

    def ensure_notification(
        self,
        task_id: str,
        *,
        kind: str,
        owner: str,
        title: str,
        message: str,
        deep_link: str,
        dedupe_key: str,
        instance_id: str | None = None,
        approval_id: str | None = None,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        if owner not in {"human", "master"}:
            raise HarnessError("VALIDATION_ERROR", "The inbox owner is invalid.")
        digest = hashlib.sha256(f"{task_id}:{dedupe_key}".encode()).hexdigest()
        inbox_id = f"in_{digest[:24]}"
        inbox_lock = self.store.layout.control_root / "locks" / f"inbox-{task_id}.lock"
        with FileLock(inbox_lock, self.store.lock_timeout_seconds):
            existing = self.store.inbox.get(task_id, inbox_id)
            if existing is not None:
                if existing["dedupe_key"] != dedupe_key:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The inbox identity belongs to a different notification.",
                    )
                return deepcopy(existing)
            now = utc_now()
            payload = {
                "schema_version": "1.0",
                "inbox_id": inbox_id,
                "task_id": task_id,
                "instance_id": instance_id,
                "approval_id": approval_id,
                "kind": kind,
                "owner": owner,
                "status": "UNREAD",
                "title": title,
                "message": message,
                "deep_link": deep_link,
                "created_at": now,
                "sequence": self._next_sequence(),
                "revision": 1,
                "dedupe_key": dedupe_key,
            }
            self.store.inbox.put(
                task_id,
                inbox_id,
                payload,
                expected_revision=0,
                actor=Actor("system", "notification_reducer"),
                command="ensure_notification",
                idempotency_key=f"ensure-{inbox_id}",
            )
            return deepcopy(payload)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        task_id = self._task_for_object("approvals", approval_id)
        approval = self.store.approval.get(task_id, approval_id)
        if approval is None:
            raise HarnessError("VALIDATION_ERROR", "The requested approval does not exist.")
        workspace = self.store.layout.workspace_root / "tasks" / task_id
        payload_path = resolve_task_path(
            workspace,
            approval["payload_ref"],
            allowed_prefixes=("approvals",),
        )
        payload = read_json(payload_path)
        return {
            "approval": deepcopy(approval),
            "approval_revision": self.store.approval.revision(task_id, approval_id),
            "payload": payload,
        }

    def list_approvals(
        self,
        *,
        task_id: str | None = None,
        instance_id: str | None = None,
        owner: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        if task_id is not None:
            validate_identifier(task_id, "task_id")
            task_ids = [task_id]
        else:
            task_ids = [
                item.name
                for item in sorted((self.store.layout.control_root / "tasks").iterdir())
                if item.is_dir()
            ]
        values: list[dict[str, Any]] = []
        for current_task_id in task_ids:
            for approval in self.store.approval.list(current_task_id):
                if instance_id is not None and approval["instance_id"] != instance_id:
                    continue
                if owner is not None and approval["owner"] != owner:
                    continue
                if status is not None and approval["status"] != status:
                    continue
                values.append(
                    {
                        **deepcopy(approval),
                        "store_revision": self.store.approval.revision(
                            current_task_id, approval["approval_id"]
                        ),
                    }
                )
        values.sort(key=lambda item: (item["created_at"], item["sequence"], item["approval_id"]))
        return values

    def list_inbox(
        self,
        *,
        owner: str,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[dict[str, Any]]:
        if owner not in {"human", "master"} or status not in {
            None,
            "UNREAD",
            "READ",
            "HANDLED",
        }:
            raise HarnessError("VALIDATION_ERROR", "The inbox filter is invalid.")
        if limit is not None and (limit < 1 or limit > 200):
            raise HarnessError("VALIDATION_ERROR", "The inbox limit is invalid.")
        values: list[dict[str, Any]] = []
        task_root = self.store.layout.control_root / "tasks"
        for task_dir in sorted(task_root.iterdir() if task_root.exists() else []):
            if not task_dir.is_dir():
                continue
            for item in self.store.inbox.list(task_dir.name):
                if item["owner"] != owner or (status is not None and item["status"] != status):
                    continue
                values.append(
                    {
                        **deepcopy(item),
                        "store_revision": self.store.inbox.revision(
                            task_dir.name, item["inbox_id"]
                        ),
                    }
                )
        values.sort(key=lambda item: (item["created_at"], item["sequence"], item["inbox_id"]))
        return values if limit is None else values[:limit]

    def commit_resolution(
        self,
        approval_id: str,
        decision: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        details = self.get_approval(approval_id)
        approval = details["approval"]
        task_id = approval["task_id"]
        request = {"approval_id": approval_id, "decision": decision}
        request_sha256 = digest_json(request)
        committed = self.store.lookup_committed_command_result(
            task_id,
            envelope.idempotency_key,
            "resolve_approval",
            request_sha256,
        )
        if committed is not None:
            return committed
        if decision not in {"APPROVED", "REJECTED"}:
            raise HarnessError("VALIDATION_ERROR", "The approval decision is invalid.")
        if approval["status"] != "PENDING":
            raise HarnessError(
                "INVALID_STATE_TRANSITION", "Only a pending approval may be resolved."
            )
        if envelope.actor_type != approval["owner"]:
            raise HarnessError(
                "VALIDATION_ERROR", "The approval must be resolved by its frozen owner."
            )
        if envelope.expected_revision != details["approval_revision"]:
            raise HarnessError(
                "REVISION_CONFLICT",
                "The approval revision changed before the decision committed.",
                {
                    "expected_revision": envelope.expected_revision,
                    "actual_revision": details["approval_revision"],
                },
            )
        now = utc_now()
        updated = {
            **approval,
            "status": decision,
            "revision": approval["revision"] + 1,
            "resolved_at": now,
            "resolved_by_type": envelope.actor_type,
            "resolved_by_id": envelope.actor_id,
        }
        result = {
            "approval": deepcopy(updated),
            "approval_revision": details["approval_revision"] + 1,
        }
        self.store.approval.put(
            task_id,
            approval_id,
            updated,
            expected_revision=details["approval_revision"],
            actor=Actor(envelope.actor_type, envelope.actor_id),
            command="resolve_approval",
            idempotency_key=envelope.idempotency_key,
            command_result=result,
            request_sha256=request_sha256,
        )
        return result

    def update_inbox_status(
        self,
        inbox_id: str,
        target: str,
        envelope: CommandEnvelope,
        *,
        enforce_owner: bool = True,
    ) -> dict[str, Any]:
        task_id = self._task_for_object("inbox", inbox_id)
        wrapper = self.store.inbox.read_wrapper(task_id, inbox_id)
        if wrapper is None:
            raise HarnessError("VALIDATION_ERROR", "The inbox item does not exist.")
        current = wrapper["payload"]
        request = {"inbox_id": inbox_id, "target": target}
        request_sha256 = digest_json(request)
        committed = self.store.lookup_committed_command_result(
            task_id,
            envelope.idempotency_key,
            "update_inbox_status",
            request_sha256,
        )
        if committed is not None:
            return committed
        if target not in {"READ", "HANDLED"} or current["status"] == "HANDLED":
            raise HarnessError("INVALID_STATE_TRANSITION", "The inbox transition is invalid.")
        if enforce_owner and envelope.actor_type != current["owner"]:
            raise HarnessError("VALIDATION_ERROR", "The inbox item belongs to another owner.")
        if envelope.expected_revision != wrapper["revision"]:
            raise HarnessError(
                "REVISION_CONFLICT",
                "The inbox revision changed before the command committed.",
                {
                    "expected_revision": envelope.expected_revision,
                    "actual_revision": wrapper["revision"],
                },
            )
        now = utc_now()
        updated = {
            **current,
            "status": target,
            "revision": current["revision"] + 1,
            "read_at": current.get("read_at", now),
            "read_by_type": current.get("read_by_type", envelope.actor_type),
            "read_by_id": current.get("read_by_id", envelope.actor_id),
        }
        if target == "HANDLED":
            updated.update(
                {
                    "handled_at": now,
                    "handled_by_type": envelope.actor_type,
                    "handled_by_id": envelope.actor_id,
                }
            )
        result = {"item": deepcopy(updated), "inbox_revision": wrapper["revision"] + 1}
        self.store.inbox.put(
            task_id,
            inbox_id,
            updated,
            expected_revision=wrapper["revision"],
            actor=Actor(envelope.actor_type, envelope.actor_id),
            command="update_inbox_status",
            idempotency_key=envelope.idempotency_key,
            command_result=result,
            request_sha256=request_sha256,
        )
        return result

    def handle_approval_notification(
        self, approval_id: str, actor: Actor, idempotency_key: str
    ) -> dict[str, Any] | None:
        matches = [
            item
            for item in self.list_inbox(owner=actor.actor_type, limit=200)
            if item["approval_id"] == approval_id
        ]
        if not matches:
            return None
        item = matches[0]
        if item["status"] == "HANDLED":
            return item
        return self.update_inbox_status(
            item["inbox_id"],
            "HANDLED",
            CommandEnvelope(
                idempotency_key=idempotency_key,
                actor_type=cast(
                    Literal["human", "master", "system", "adapter"], actor.actor_type
                ),
                actor_id=actor.actor_id,
                expected_revision=item["store_revision"],
            ),
        )["item"]

    def reconcile_terminal_notifications(self) -> None:
        """Rebuild idempotent terminal notifications after any crash window."""

        tasks_root = self.store.layout.control_root / "tasks"
        for task_directory in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
            if not task_directory.is_dir():
                continue
            task_id = task_directory.name
            plan = self.store.plan.get(task_id, task_id)
            if plan is None:
                continue
            for instance in plan["instances"]:
                status = instance["status"]
                if status == "SUCCEEDED":
                    kind = "INSTANCE_SUCCEEDED"
                    title = "Agent 交付已发布"
                    message = f"实例 {instance['instance_id']} 的必需交付已校验并发布。"
                    dedupe_key = f"instance-succeeded:{instance['instance_id']}"
                elif status == "FAILED" and isinstance(
                    instance.get("delivery_rejection"), dict
                ):
                    rejection = instance["delivery_rejection"]
                    kind = "INSTANCE_DELIVERY_REJECTED"
                    title = "Agent 交付未通过发布校验"
                    message = (
                        f"实例 {instance['instance_id']} 的交付已隔离。"
                        "修复后重新校验不会重跑模型步骤。"
                    )
                    rejection_identity = {
                        key: rejection[key] for key in ("code", "message", "details")
                    }
                    dedupe_key = (
                        f"instance-delivery-rejected:{instance['instance_id']}:"
                        f"{digest_json(rejection_identity)[:20]}"
                    )
                elif status in {"FAILED", "FAILED_TO_START"}:
                    kind = "INSTANCE_FAILED"
                    title = "Agent 执行失败"
                    message = f"实例 {instance['instance_id']} 已进入失败状态。"
                    dedupe_key = f"instance-failed:{instance['instance_id']}"
                elif status == "CRASHED":
                    kind = "INSTANCE_CRASHED"
                    title = "Agent 进程异常退出"
                    message = f"实例 {instance['instance_id']} 的受监管进程已崩溃。"
                    dedupe_key = f"instance-crashed:{instance['instance_id']}"
                else:
                    continue
                self.ensure_notification(
                    task_id,
                    kind=kind,
                    owner="human",
                    title=title,
                    message=message,
                    deep_link=f"instances/{instance['instance_id']}",
                    dedupe_key=dedupe_key,
                    instance_id=instance["instance_id"],
                )
            task = plan["task"]
            if task["status"] in {"SUCCEEDED", "PARTIAL"}:
                self.ensure_notification(
                    task_id,
                    kind="TASK_SUCCEEDED",
                    owner="human",
                    title="主任务已完成",
                    message=f"任务 {task_id} 的必需阶段均已完成。",
                    deep_link=f"tasks/{task_id}",
                    dedupe_key=f"task-complete:{task_id}:{task['plan_revision']}",
                )
            elif task["status"] == "FAILED":
                self.ensure_notification(
                    task_id,
                    kind="TASK_FAILED",
                    owner="human",
                    title="主任务失败",
                    message=f"任务 {task_id} 已进入失败状态。",
                    deep_link=f"tasks/{task_id}",
                    dedupe_key=f"task-failed:{task_id}:{task['plan_revision']}",
                )

    def _next_sequence(self) -> int:
        with FileLock(self.sequence_lock, self.store.lock_timeout_seconds):
            current = read_json(self.sequence_path) if self.sequence_path.exists() else {"value": 0}
            value = int(current["value"]) + 1
            atomic_write_json(self.sequence_path, {"value": value})
            return value

    def _task_for_object(self, directory: str, object_id: str) -> str:
        validate_identifier(object_id, f"{directory}_id")
        matches = [
            path.parent.parent.name
            for path in self.store.layout.control_root.glob(
                f"tasks/*/{directory}/{object_id}.json"
            )
            if path.is_file()
        ]
        if len(matches) != 1:
            raise HarnessError("VALIDATION_ERROR", "The requested control object is not unique.")
        return matches[0]
