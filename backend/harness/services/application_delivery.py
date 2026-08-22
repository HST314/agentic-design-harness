"""Approval, observation and delivery application use cases."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now

CrashHook = Callable[[str], None]
_DELIVERY_REJECTION_CODES = {
    "ASSET_CORRUPTED",
    "ASSET_VALIDATION_FAILED",
    "VALIDATION_ERROR",
}


class ApplicationDeliveryMixin:
    """Feature module mixed into the application service without changing its API."""

    if TYPE_CHECKING:
        def __getattr__(self, name: str) -> Any: ...

    def observe_instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        """Poll one Agent and persist its deterministic domain-state projection."""

        instance = self._instance(task_id, instance_id)
        if instance["status"] not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
            return {"instance": instance, "observation": None, "transition": None}
        adapter = self.adapters.get(instance["agent_type"])
        observation = adapter.get_status(instance_id)
        if observation.status not in {"RUNNING", "WAITING_APPROVAL", "FAILED"}:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Agent adapter returned a non-projectable status.",
                {"status": observation.status},
            )
        if observation.details.get("completed") is True:
            try:
                delivery = self._collect_publish_and_complete(
                    task_id, instance_id, adapter, observation
                )
            except HarnessError as exc:
                if exc.code not in _DELIVERY_REJECTION_CODES:
                    raise
                return self._record_delivery_rejection(
                    task_id,
                    instance_id,
                    observation,
                    exc,
                )
            return {
                "instance": deepcopy(delivery["instance"]),
                "observation": {
                    "status": observation.status,
                    "step_id": observation.step_id,
                    "capabilities": list(observation.capabilities),
                    "details": deepcopy(observation.details),
                },
                "transition": delivery["transition"],
                "approval": None,
                "delivery": delivery,
            }
        transition = None
        if observation.status != instance["status"]:
            task_revision = self.store.task.revision(task_id, task_id)
            observation_digest = digest_json(
                {
                    "status": observation.status,
                    "step_id": observation.step_id,
                    "capabilities": list(observation.capabilities),
                    "details": observation.details,
                }
            )
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                observation.status,
                CommandEnvelope(
                    idempotency_key=(
                        f"observe-{instance_id}-{task_revision}-{observation_digest[:20]}"
                    ),
                    actor_type="adapter",
                    actor_id=f"{instance['agent_type']}_adapter",
                    expected_revision=task_revision,
                ),
            )
            instance = next(
                item
                for item in transition["plan"]["instances"]
                if item["instance_id"] == instance_id
            )
        approval = None
        if observation.status == "WAITING_APPROVAL":
            operation_id = str(
                observation.details.get("job_id")
                or digest_json(
                    {
                        "instance_id": instance_id,
                        "step_id": observation.step_id,
                        "details": observation.details,
                    }
                )[:24]
            )
            approval = self.approvals.ensure_workflow_approval(
                task_id,
                instance_id,
                step_id=str(observation.step_id),
                capabilities=list(observation.capabilities),
                context=deepcopy(observation.details.get("approval_context") or {}),
                operation_id=operation_id,
            )
        elif observation.status == "FAILED":
            self.approvals.ensure_notification(
                task_id,
                kind="INSTANCE_FAILED",
                owner="human",
                title="Image Agent 执行失败",
                message=f"实例 {instance_id} 已进入失败状态。请查看实例详情。",
                deep_link=f"instances/{instance_id}",
                dedupe_key=(f"instance-failed:{instance_id}"),
                instance_id=instance_id,
            )
        return {
            "instance": deepcopy(instance),
            "observation": {
                "status": observation.status,
                "step_id": observation.step_id,
                "capabilities": list(observation.capabilities),
                "details": deepcopy(observation.details),
            },
            "transition": transition,
            "approval": approval,
        }

    def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        action: str | None,
        payload: dict[str, Any],
        operation_id: str,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        validate_identifier(approval_id, "approval_id")
        validate_identifier(operation_id, "operation_id")
        initial_approval = self.approvals.get_approval(approval_id)
        task_id = initial_approval["approval"]["task_id"]
        request = {
            "approval_id": approval_id,
            "decision": decision,
            "action": action,
            "payload": deepcopy(payload),
            "envelope": envelope.model_dump(mode="json"),
        }
        request_sha256 = digest_json(request)
        intent_path = self._intent_path(operation_id)
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            FileLock(self._intent_lock(operation_id), self.store.lock_timeout_seconds),
        ):
            if intent_path.exists():
                intent = read_json(intent_path)
                if (
                    intent.get("kind") != "RESOLVE_APPROVAL"
                    or intent.get("request_sha256") != request_sha256
                ):
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
                approval_details = self.approvals.get_approval(approval_id)
                approval = approval_details["approval"]
                approval_payload = approval_details["payload"]
                if approval["status"] != "PENDING":
                    raise HarnessError(
                        "INVALID_STATE_TRANSITION", "Only a pending approval may be resolved."
                    )
                if envelope.actor_type != approval["owner"]:
                    raise HarnessError(
                        "VALIDATION_ERROR", "The approval must be resolved by its frozen owner."
                    )
                if envelope.expected_revision != approval_details["approval_revision"]:
                    raise HarnessError(
                        "REVISION_CONFLICT",
                        "The approval revision changed before the decision was prepared.",
                        {
                            "expected_revision": envelope.expected_revision,
                            "actual_revision": approval_details["approval_revision"],
                        },
                    )
                if decision == "APPROVED":
                    if action not in approval_payload["available_actions"]:
                        raise HarnessError(
                            "VALIDATION_ERROR",
                            "The selected action is not available for this approval.",
                        )
                elif decision == "REJECTED":
                    if action is not None or payload:
                        raise HarnessError(
                            "VALIDATION_ERROR", "A rejected approval cannot submit an Agent action."
                        )
                else:
                    raise HarnessError("VALIDATION_ERROR", "The approval decision is invalid.")
                intent = {
                    "schema_version": "1.0",
                    "kind": "RESOLVE_APPROVAL",
                    "operation_id": operation_id,
                    "request_sha256": request_sha256,
                    "request": request,
                    "task_id": task_id,
                    "instance_id": approval["instance_id"],
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                    "advance": None,
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_approval_intent")
            return self._resume_approval(intent_path, crash_hook)

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
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            instance = self._instance(task_id, instance_id)
            if instance["status"] == "SUCCEEDED":
                return self._replay_completed_delivery(
                    task_id,
                    instance_id,
                    source_relative_path=source_relative_path,
                    role=role,
                    description=description,
                    operation_id=operation_id,
                    envelope=envelope,
                )
            self._require_delivery_command(task_id, instance_id, envelope)
            batch_id = self._manual_delivery_batch_id(task_id, instance_id)
            self.assets.inspect_delivery(
                task_id,
                instance_id,
                source_relative_path=source_relative_path,
                role=role,
                description=description,
            )
            manifest = self.assets.publish_delivery(
                task_id,
                instance_id,
                source_relative_path=source_relative_path,
                role=role,
                description=description,
                idempotency_key=operation_id,
                batch_id=batch_id,
            )
            prepared = self.assets.list_publication_batch(task_id, instance_id, batch_id)
            declared = self._declared_delivery_candidates(task_id, instance_id, prepared)
            transition = None
            if self._required_delivery_selection(task_id, instance_id, declared) is not None:
                self.assets.commit_publication_batch(
                    task_id,
                    instance_id,
                    batch_id=batch_id,
                    manifests=declared,
                )
                transition = self._complete_instance_if_ready(
                    task_id,
                    instance_id,
                    envelope=envelope,
                    require_deliveries=True,
                    candidate_manifests=declared,
                )
            return {
                "manifest": manifest,
                "complete": transition is not None,
                "transition": transition,
            }

    def _replay_completed_delivery(
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
        instance = self._instance(task_id, instance_id)
        self._require_owning_adapter(instance, envelope)
        manifest = self.assets.replay_delivery(
            task_id,
            instance_id,
            source_relative_path=source_relative_path,
            role=role,
            description=description,
            idempotency_key=operation_id,
            batch_id=self._manual_delivery_batch_id(task_id, instance_id),
        )
        if manifest is None:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A succeeded instance only accepts an exact delivery replay.",
                {"instance_id": instance_id},
            )
        transition = self.commands.transition_instance(task_id, instance_id, "SUCCEEDED", envelope)
        self._ensure_success_notifications(task_id, instance_id, transition)
        return {"manifest": manifest, "complete": True, "transition": transition}

    def _resume_approval(self, intent_path: Path, crash_hook: CrashHook | None) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        envelope = CommandEnvelope.model_validate(request["envelope"])
        task_id = intent["task_id"]
        instance_id = intent["instance_id"]
        instance = self._instance(task_id, instance_id)
        if request["decision"] == "APPROVED" and intent["advance"] is None:
            adapter = self.adapters.get(instance["agent_type"])
            advance_payload = deepcopy(request["payload"])
            advance_payload["actor"] = envelope.actor_id
            advance = adapter.request_advance(
                instance_id,
                str(request["action"]),
                advance_payload,
                self._derived_id("advance", intent["operation_id"], request["approval_id"]),
            )
            intent["advance"] = {
                "accepted": advance.accepted,
                "operation_id": advance.operation_id,
                "details": deepcopy(advance.details),
            }
            intent["state"] = "ADVANCE_ACCEPTED" if advance.accepted else "ADVANCE_REJECTED"
            atomic_write_json(intent_path, intent)
            if crash_hook:
                crash_hook("after_adapter_advance")
        if request["decision"] == "APPROVED" and not intent["advance"]["accepted"]:
            error = HarnessError(
                "INVALID_STATE_TRANSITION",
                "The Agent adapter rejected the approval advance; no decision was committed.",
                {
                    "approval_id": request["approval_id"],
                    "instance_id": instance_id,
                    "operation_id": intent["advance"]["operation_id"],
                },
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
                }
            )
            atomic_write_json(intent_path, intent)
            raise error
        target_status = "RUNNING" if request["decision"] == "APPROVED" else "FAILED"
        instance = self._instance(task_id, instance_id)
        if instance["status"] != target_status:
            if instance["status"] != "WAITING_APPROVAL":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "The approval no longer owns a waiting Agent instance.",
                    {"current": instance["status"]},
                )
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                target_status,
                CommandEnvelope(
                    idempotency_key=self._derived_id(
                        "decision", intent["operation_id"], request["approval_id"]
                    ),
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    expected_revision=self.store.task.revision(task_id, task_id),
                ),
            )
            instance = next(
                item
                for item in transition["plan"]["instances"]
                if item["instance_id"] == instance_id
            )
            if crash_hook:
                crash_hook("after_approval_instance_transition")
        resolution = self.approvals.commit_resolution(
            request["approval_id"], request["decision"], envelope
        )
        if crash_hook:
            crash_hook("after_approval_commit")
        actor = Actor(envelope.actor_type, envelope.actor_id)
        self.approvals.handle_approval_notification(
            request["approval_id"],
            actor,
            self._derived_id("handled", intent["operation_id"], request["approval_id"]),
        )
        if request["decision"] == "REJECTED":
            self.approvals.ensure_notification(
                task_id,
                kind="INSTANCE_FAILED",
                owner="human",
                title="工作流决议已拒绝",
                message=f"实例 {instance_id} 因审批拒绝已停止。",
                deep_link=f"instances/{instance_id}",
                dedupe_key=f"instance-failed:{instance_id}",
                instance_id=instance_id,
                approval_id=request["approval_id"],
            )
        if crash_hook:
            crash_hook("after_approval_notification")
        result = {
            "approval": resolution["approval"],
            "approval_revision": resolution["approval_revision"],
            "instance": deepcopy(instance),
            "advance": deepcopy(intent["advance"]),
        }
        intent.update({"state": "COMMITTED", "committed_at": utc_now(), "result": result})
        atomic_write_json(intent_path, intent)
        return result

    def _collect_publish_and_complete(
        self,
        task_id: str,
        instance_id: str,
        adapter,
        observation,
    ) -> dict[str, Any]:
        deliveries = adapter.collect_deliveries(instance_id)
        if not deliveries:
            raise HarnessError(
                "VALIDATION_ERROR", "A completed Image Agent did not expose a delivery."
            )
        envelope = CommandEnvelope(
            idempotency_key=(
                f"complete-{instance_id}-"
                f"{digest_json(sorted(item['sha256'] for item in deliveries))[:20]}"
            ),
            actor_type="adapter",
            actor_id=f"{self._instance(task_id, instance_id)['agent_type']}_adapter",
            expected_revision=self.store.task.revision(task_id, task_id),
        )
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            self._require_delivery_command(task_id, instance_id, envelope)
            candidates = [
                self.assets.inspect_delivery(
                    task_id,
                    instance_id,
                    source_relative_path=delivery["source_relative_path"],
                    role=delivery["role"],
                    description=delivery["description"],
                    expected_sha256=delivery.get("sha256"),
                    derivation=delivery.get("derivation"),
                )
                for delivery in deliveries
            ]
            self._validate_required_delivery_set(task_id, instance_id, candidates)
            batch_payload = sorted(
                candidates,
                key=lambda item: (
                    item["role"],
                    item["kind"],
                    item["mime_type"],
                    item["sha256"],
                    item["source_relative_path"],
                ),
            )
            batch_id = f"batch_{digest_json(batch_payload)[:24]}"
            manifests = []
            for delivery, candidate in zip(deliveries, candidates, strict=True):
                operation_id = f"collect-{instance_id}-{digest_json(candidate)[:32]}"
                manifests.append(
                    self.assets.publish_delivery(
                        task_id,
                        instance_id,
                        source_relative_path=delivery["source_relative_path"],
                        role=delivery["role"],
                        description=delivery["description"],
                        idempotency_key=operation_id,
                        batch_id=batch_id,
                        derivation=candidate["derivation"],
                    )
                )
            self.assets.commit_publication_batch(
                task_id,
                instance_id,
                batch_id=batch_id,
                manifests=manifests,
            )
            transition = self._complete_instance_if_ready(
                task_id,
                instance_id,
                envelope=envelope,
                require_deliveries=True,
                candidate_manifests=manifests,
            )
            assert transition is not None
        instance = next(
            item for item in transition["plan"]["instances"] if item["instance_id"] == instance_id
        )
        return {
            "instance": instance,
            "manifests": manifests,
            "transition": transition,
            "observation": {
                "step_id": observation.step_id,
                "details": deepcopy(observation.details),
            },
        }

    def _record_delivery_rejection(
        self,
        task_id: str,
        instance_id: str,
        observation,
        error: HarnessError,
    ) -> dict[str, Any]:
        current = self._instance(task_id, instance_id)
        if current["status"] not in {"RUNNING", "WAITING_APPROVAL"}:
            raise error
        rejection = {
            "code": error.code,
            "message": error.message[:1000],
            "details": deepcopy(error.details),
        }
        revision = self.store.task.revision(task_id, task_id)
        transition = self.commands.reject_instance_delivery(
            task_id,
            instance_id,
            rejection,
            CommandEnvelope(
                idempotency_key=f"reject-delivery-{digest_json(rejection)[:32]}",
                actor_type="adapter",
                actor_id=f"{current['agent_type']}_adapter",
                expected_revision=revision,
            ),
        )
        instance = next(
            item for item in transition["plan"]["instances"] if item["instance_id"] == instance_id
        )
        self.approvals.ensure_notification(
            task_id,
            kind="INSTANCE_DELIVERY_REJECTED",
            owner="human",
            title="Agent 交付未通过发布校验",
            message=f"实例 {instance_id} 的交付已隔离。修复后重新校验不会重跑模型步骤。",
            deep_link=f"instances/{instance_id}",
            dedupe_key=f"instance-delivery-rejected:{instance_id}:{digest_json(rejection)[:20]}",
            instance_id=instance_id,
        )
        return {
            "instance": deepcopy(instance),
            "observation": {
                "status": observation.status,
                "step_id": observation.step_id,
                "capabilities": list(observation.capabilities),
                "details": deepcopy(observation.details),
            },
            "transition": transition,
            "approval": None,
            "delivery": {"status": "REJECTED", "rejection": rejection},
        }

    def _require_delivery_command(
        self,
        task_id: str,
        instance_id: str,
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        instance = self._instance(task_id, instance_id)
        self._require_owning_adapter(instance, envelope)
        if instance["status"] != "RUNNING":
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "Only a running instance with no pending approval may publish a delivery.",
                {"current": instance["status"], "instance_id": instance_id},
            )
        pending = self.approvals.list_approvals(
            task_id=task_id,
            instance_id=instance_id,
            status="PENDING",
        )
        if pending:
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A pending approval must be resolved before delivery completion.",
                {
                    "instance_id": instance_id,
                    "approval_ids": [item["approval_id"] for item in pending],
                },
            )
        self.commands.validate_task_revision(task_id, envelope.expected_revision)
        return instance

    @staticmethod
    def _require_owning_adapter(instance: dict[str, Any], envelope: CommandEnvelope) -> None:
        expected_actor_id = f"{instance['agent_type']}_adapter"
        if envelope.actor_type != "adapter" or envelope.actor_id != expected_actor_id:
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only the owning Agent adapter may publish an instance delivery.",
                {
                    "actor_type": envelope.actor_type,
                    "actor_id": envelope.actor_id,
                    "expected_actor_id": expected_actor_id,
                },
            )

    def _complete_instance_if_ready(
        self,
        task_id: str,
        instance_id: str,
        *,
        envelope: CommandEnvelope,
        require_deliveries: bool,
        candidate_manifests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """The sole instance-success gate for manual and collected deliveries."""

        self._require_delivery_command(task_id, instance_id, envelope)
        if not self._required_deliveries_satisfied(
            task_id,
            instance_id,
            candidate_manifests=candidate_manifests,
        ):
            if require_deliveries:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "Published Image assets do not satisfy the required delivery contract.",
                )
            return None
        transition = self.commands.transition_instance(
            task_id,
            instance_id,
            "SUCCEEDED",
            envelope,
        )
        self._ensure_success_notifications(task_id, instance_id, transition)
        return transition

    def _ensure_success_notifications(
        self,
        task_id: str,
        instance_id: str,
        transition: dict[str, Any],
    ) -> None:
        self.approvals.ensure_notification(
            task_id,
            kind="INSTANCE_SUCCEEDED",
            owner="human",
            title="Agent 交付已发布",
            message=f"实例 {instance_id} 的必需交付已校验并发布。",
            deep_link=f"tasks/{task_id}?tab=resources",
            dedupe_key=f"instance-succeeded:{instance_id}",
            instance_id=instance_id,
        )
        if transition["task"]["status"] in {"SUCCEEDED", "PARTIAL"}:
            self.approvals.ensure_notification(
                task_id,
                kind="TASK_SUCCEEDED",
                owner="human",
                title="主任务已完成",
                message=f"任务 {task_id} 的必需阶段均已完成。",
                deep_link=f"tasks/{task_id}",
                dedupe_key=(f"task-complete:{task_id}:" f"{transition['task']['plan_revision']}"),
            )

    def _validate_required_delivery_set(
        self,
        task_id: str,
        instance_id: str,
        candidates: list[dict[str, Any]],
    ) -> None:
        plan = self._plan(task_id)
        card = next(item for item in plan["task_cards"] if item["instance_id"] == instance_id)
        declared = len(
            self._declared_delivery_candidates(task_id, instance_id, candidates)
        ) == len(candidates)
        if declared and self._required_deliveries_satisfied(
            task_id,
            instance_id,
            candidate_manifests=candidates,
        ):
            return
        raise HarnessError(
            "VALIDATION_ERROR",
            "Collected Image assets do not satisfy the required delivery contract.",
            {
                "required": [
                    deepcopy(item) for item in card["expected_deliveries"] if item["required"]
                ],
                "observed": [
                    {
                        "kind": item["kind"],
                        "role": item["role"],
                        "mime_type": item["mime_type"],
                        "sha256": item["sha256"],
                    }
                    for item in candidates
                ],
            },
        )

    def _required_deliveries_satisfied(
        self,
        task_id: str,
        instance_id: str,
        *,
        candidate_manifests: list[dict[str, Any]] | None = None,
    ) -> bool:
        published = candidate_manifests
        if published is None:
            published = [
                self.assets.verify_asset(task_id, item["manifest"]["asset_id"])
                for item in self.assets.list_assets(task_id)
                if item["integrity_status"] == "VERIFIED"
                and item["manifest"].get("producer_instance_id") == instance_id
            ]
        return self._required_delivery_selection(task_id, instance_id, published) is not None

    def _required_delivery_selection(
        self,
        task_id: str,
        instance_id: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        plan = self._plan(task_id)
        card = next(
            item for item in plan["task_cards"] if item["instance_id"] == instance_id
        )
        remaining = list(candidates)
        selected: list[dict[str, Any]] = []
        for expected in card["expected_deliveries"]:
            if not expected["required"]:
                continue
            match = next(
                (
                    index
                    for index, candidate in enumerate(remaining)
                    if self._delivery_matches(candidate, expected)
                ),
                None,
            )
            if match is None:
                return None
            selected.append(remaining.pop(match))
        return selected

    def _declared_delivery_candidates(
        self,
        task_id: str,
        instance_id: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        plan = self._plan(task_id)
        card = next(
            item for item in plan["task_cards"] if item["instance_id"] == instance_id
        )
        return [
            candidate
            for candidate in candidates
            if any(
                self._delivery_matches(candidate, expected)
                for expected in card["expected_deliveries"]
            )
        ]

    @staticmethod
    def _manual_delivery_batch_id(task_id: str, instance_id: str) -> str:
        digest = hashlib.sha256(f"{task_id}:{instance_id}".encode()).hexdigest()
        return f"batch_manual_{digest[:20]}"

    @staticmethod
    def _delivery_matches(candidate: dict[str, Any], expected: dict[str, Any]) -> bool:
        return (
            candidate["role"] == expected["role"]
            and candidate["kind"] == expected["kind"]
            and candidate["mime_type"] in expected["accepted_mime_types"]
        )
