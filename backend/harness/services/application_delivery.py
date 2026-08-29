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
        observed_revision = self.store.task.revision(task_id, task_id)
        adapter = self.adapters.get(instance["agent_type"])
        observation = adapter.get_status(instance_id)
        config_application = None
        if instance["agent_type"] == "image":
            config_application = self.runtime_settings.apply_pending_if_safe(
                task_id, instance_id
            )
            if (
                config_application is not None
                and config_application["status"] == "APPLIED_ON_BRANCH"
            ):
                observation = adapter.get_status(instance_id)
        if observation.status not in {"RUNNING", "WAITING_APPROVAL", "FAILED"}:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The Agent adapter returned a non-projectable status.",
                {"status": observation.status},
            )
        # Polling is intentionally outside the locks. Only persistence is serialized,
        # then fenced by the revision captured before the poll. This avoids blocking a
        # human decision on Agent I/O while preventing an older observation from landing.
        with (
            FileLock(self._task_lock(task_id), self.store.lock_timeout_seconds),
            self.commands.task_guard(task_id),
        ):
            instance = self._instance(task_id, instance_id)
            if (
                self.store.task.revision(task_id, task_id) != observed_revision
                or instance["status"]
                not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}
            ):
                return {
                    "instance": deepcopy(instance),
                    "observation": {
                        "status": observation.status,
                        "step_id": observation.step_id,
                        "capabilities": list(observation.capabilities),
                        "details": deepcopy(observation.details),
                        "stale": True,
                    },
                    "transition": None,
                    "approval": None,
                    "config_application": config_application,
                }
            return self._project_observation_locked(
                task_id,
                instance_id,
                instance,
                adapter,
                observation,
                config_application,
            )

    def _project_observation_locked(
        self,
        task_id: str,
        instance_id: str,
        instance: dict[str, Any],
        adapter: Any,
        observation: Any,
        config_application: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Persist a current observation while both task guards are held."""

        adapter_actor_id = f"{instance['agent_type']}_adapter"
        observation_operation_id = str(
            observation.details.get("job_id")
            or digest_json(
                {
                    "instance_id": instance_id,
                    "step_id": observation.step_id,
                    "details": observation.details,
                }
            )[:24]
        )
        if observation.status == "WAITING_APPROVAL":
            resolved_gate = self.approvals.workflow_approval(
                task_id,
                instance_id,
                step_id=str(observation.step_id),
                operation_id=observation_operation_id,
            )
            if resolved_gate is not None and resolved_gate["status"] != "PENDING":
                transition = None
                pending = [
                    item
                    for item in self.approvals.list_approvals(
                        task_id=task_id,
                        instance_id=instance_id,
                        status="PENDING",
                    )
                    if item["kind"] == "WORKFLOW"
                ]
                # Repair instances stranded by older versions of the observer. Do not
                # disturb a newer, genuinely pending workflow gate for the same instance.
                if instance["status"] == "WAITING_APPROVAL" and not pending:
                    transition = self.commands.transition_instance(
                        task_id,
                        instance_id,
                        "RUNNING",
                        CommandEnvelope(
                            idempotency_key=(
                                f"discard-stale-gate-{resolved_gate['approval_id']}-"
                                f"{self.store.task.revision(task_id, task_id)}"
                            ),
                            actor_type="adapter",
                            actor_id=f"{instance['agent_type']}_adapter",
                            expected_revision=self.store.task.revision(task_id, task_id),
                        ),
                    )
                    instance = next(
                        item
                        for item in transition["plan"]["instances"]
                        if item["instance_id"] == instance_id
                    )
                return {
                    "instance": deepcopy(instance),
                    "observation": {
                        "status": observation.status,
                        "step_id": observation.step_id,
                        "capabilities": list(observation.capabilities),
                        "details": deepcopy(observation.details),
                        "stale": True,
                    },
                    "transition": transition,
                    "approval": None,
                    "config_application": config_application,
                }
        self.approvals.acknowledge_external_workflow_approvals(
            task_id,
            instance_id,
            current_step_id=(
                str(observation.step_id)
                if observation.status == "WAITING_APPROVAL"
                else None
            ),
            current_operation_id=(
                observation_operation_id
                if observation.status == "WAITING_APPROVAL"
                else None
            ),
            actor_id=adapter_actor_id,
        )
        if observation.details.get("completed") is True:
            if instance["agent_type"] == "ppt":
                delivery = self._collect_declared_deliveries_and_complete(
                    task_id, instance_id, adapter, observation
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
                    "config_application": config_application,
                }
            try:
                delivery = self._collect_bundles_and_request_review(
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
                "config_application": config_application,
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
            approval = self.approvals.ensure_workflow_approval(
                task_id,
                instance_id,
                step_id=str(observation.step_id),
                capabilities=list(observation.capabilities),
                context=deepcopy(observation.details.get("approval_context") or {}),
                operation_id=observation_operation_id,
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
            "config_application": config_application,
        }

    def _collect_declared_deliveries_and_complete(
        self, task_id: str, instance_id: str, adapter, observation
    ) -> dict[str, Any]:
        """Publish a completed non-bundle Agent's declared delivery set."""

        current = self._instance(task_id, instance_id)
        resume_transition = None
        if current["status"] == "WAITING_APPROVAL":
            resume_transition = self.commands.transition_instance(
                task_id,
                instance_id,
                "RUNNING",
                CommandEnvelope(
                    idempotency_key=(
                        f"observe-resume-{instance_id}-"
                        f"{digest_json(observation.details)[:24]}"
                    ),
                    actor_type="adapter",
                    actor_id=f"{current['agent_type']}_adapter",
                    expected_revision=self.store.task.revision(task_id, task_id),
                ),
            )
        candidates = adapter.collect_deliveries(instance_id)
        if not isinstance(candidates, list) or not candidates:
            raise HarnessError(
                "VALIDATION_ERROR", "A completed Agent did not expose its declared delivery."
            )
        self._validate_required_delivery_set(task_id, instance_id, candidates)
        transition = None
        manifests: list[dict[str, Any]] = []
        for candidate in candidates:
            inspected = self.assets.inspect_delivery(
                task_id,
                instance_id,
                source_relative_path=candidate["source_relative_path"],
                role=candidate["role"],
                description=candidate["description"],
                expected_sha256=candidate["sha256"],
                derivation=candidate.get("derivation"),
            )
            result = self.publish_delivery_and_complete(
                task_id,
                instance_id,
                source_relative_path=inspected["source_relative_path"],
                role=inspected["role"],
                description=inspected["description"],
                operation_id=(
                    f"publish-{instance_id}-{candidate['sha256'][:24]}"
                ),
                envelope=CommandEnvelope(
                    idempotency_key=(
                        f"complete-{instance_id}-{candidate['sha256'][:24]}"
                    ),
                    actor_type="adapter",
                    actor_id=f"{current['agent_type']}_adapter",
                    expected_revision=self.store.task.revision(task_id, task_id),
                ),
            )
            manifests.append(result["manifest"])
            transition = result["transition"] or transition
        return {
            "status": "PUBLISHED",
            "instance": self._instance(task_id, instance_id),
            "manifests": manifests,
            "transition": transition or resume_transition,
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
        if decision == "APPROVED":
            application = self.runtime_settings.apply_pending_if_safe(
                task_id,
                initial_approval["approval"]["instance_id"],
            )
            if application is not None and application["status"] == "WAITING_SAFE_POINT":
                raise HarnessError(
                    "SAFE_CHECKPOINT_UNAVAILABLE",
                    "The pending runtime configuration is not yet safe to apply.",
                    {"proposal_id": application["proposal_id"]},
                )
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
                    "approval_kind": approval["kind"],
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                    "advance": None,
                    "publication": None,
                    "result": None,
                }
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_approval_intent")
            if decision == "APPROVED" and intent["advance"] is None:
                self.runtime_settings.assert_no_pending_advance(
                    task_id, intent["instance_id"]
                )
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
        if intent.get("approval_kind") == "DELIVERY_REVIEW":
            return self._resume_delivery_review_approval(intent_path, crash_hook)
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

    def observe_delivery_sources(self, task_id: str) -> None:
        """Reconcile active bundle-producing instances before a delivery read.

        Observation otherwise runs only when the instance redirect page polls a
        single instance, so a completed Image Agent never surfaced its delivery
        candidates while the human stayed inside the workbench. Delivery reads
        (the workbench bridge, the delivery confirmation page) sweep here first
        so the poll itself drives reconciliation. Observation failures are
        best-effort: the projection read must stay available.
        """

        validate_identifier(task_id, "task_id")
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            return
        for instance in plan["instances"]:
            if instance["status"] not in {"STARTING", "RUNNING", "WAITING_APPROVAL"}:
                continue
            adapter = self.adapters.get_optional(instance["agent_type"])
            if adapter is None or not callable(
                getattr(adapter, "collect_delivery_bundles", None)
            ):
                continue
            try:
                self.observe_instance(task_id, instance["instance_id"])
            except HarnessError:
                continue

    def list_delivery_bundle_candidates(
        self,
        task_id: str,
        *,
        instance_id: str | None = None,
    ) -> list[dict[str, Any]]:
        validate_identifier(task_id, "task_id")
        if instance_id is not None:
            validate_identifier(instance_id, "instance_id")
        root = (
            self.store.layout.initialize_task(task_id)[1]
            / "deliveries"
            / "candidates"
        )
        candidates: list[dict[str, Any]] = []
        for path in sorted(root.glob("*.json")):
            candidate = read_json(path)
            self.store.contracts.validate("delivery-bundle-candidate", candidate)
            if (
                candidate["task_id"] != task_id
                or path.name != f"{candidate['bundle_id']}.json"
            ):
                raise HarnessError(
                    "ASSET_VALIDATION_FAILED", "A delivery candidate has an invalid owner."
                )
            if instance_id is None or candidate["instance_id"] == instance_id:
                candidates.append(candidate)
        return sorted(
            candidates,
            key=lambda item: (item["created_at"], item["branch_id"], item["bundle_id"]),
        )

    def _candidate_path(self, task_id: str, bundle_id: str) -> Path:
        validate_identifier(bundle_id, "bundle_id")
        return (
            self.store.layout.initialize_task(task_id)[1]
            / "deliveries"
            / "candidates"
            / f"{bundle_id}.json"
        )

    def _persist_delivery_bundle_candidate(
        self, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        self.store.contracts.validate("delivery-bundle-candidate", candidate)
        path = self._candidate_path(candidate["task_id"], candidate["bundle_id"])
        if path.exists():
            existing = read_json(path)
            self.store.contracts.validate("delivery-bundle-candidate", existing)
            immutable = (
                "schema_version",
                "bundle_id",
                "task_id",
                "work_item_id",
                "instance_id",
                "task_card_revision",
                "branch_id",
                "checkpoint_id",
                "image",
                "design_note",
                "created_at",
            )
            if any(existing[key] != candidate[key] for key in immutable):
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The delivery bundle identity was reused for different private outputs.",
                    {"bundle_id": candidate["bundle_id"]},
                )
            return existing
        atomic_write_json(path, candidate, mode=0o640)
        return deepcopy(candidate)

    def _decide_delivery_bundle_candidate(
        self,
        task_id: str,
        bundle_id: str,
        *,
        status: str,
        actor: dict[str, str],
        decided_at: str,
        publication_batch_id: str | None,
    ) -> dict[str, Any]:
        path = self._candidate_path(task_id, bundle_id)
        if not path.exists():
            raise HarnessError("VALIDATION_ERROR", "The delivery candidate does not exist.")
        candidate = read_json(path)
        if candidate["status"] != "PENDING_CONFIRMATION":
            expected = {
                "status": status,
                "actor": actor,
                "decided_at": decided_at,
                "publication_batch_id": publication_batch_id,
            }
            if any(candidate.get(key) != value for key, value in expected.items()):
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The delivery candidate already has another terminal decision.",
                    {"bundle_id": bundle_id},
                )
            return candidate
        updated = {
            **candidate,
            "status": status,
            "actor": deepcopy(actor),
            "decided_at": decided_at,
            "publication_batch_id": publication_batch_id,
        }
        self.store.contracts.validate("delivery-bundle-candidate", updated)
        atomic_write_json(path, updated, mode=0o640)
        return updated

    def _collect_bundles_and_request_review(
        self,
        task_id: str,
        instance_id: str,
        adapter,
        observation,
    ) -> dict[str, Any]:
        collector = getattr(adapter, "collect_delivery_bundles", None)
        if not callable(collector):
            raise HarnessError(
                "VALIDATION_ERROR", "The owning Agent adapter cannot collect delivery bundles."
            )
        raw_collected = collector(instance_id)
        if not isinstance(raw_collected, list) or any(
            not isinstance(item, dict) for item in raw_collected
        ):
            raise HarnessError(
                "VALIDATION_ERROR", "The Agent adapter returned malformed delivery bundles."
            )
        collected: list[dict[str, Any]] = raw_collected
        if not collected:
            raise HarnessError(
                "VALIDATION_ERROR", "A completed Image Agent did not expose a delivery bundle."
            )
        candidates = [self._persist_delivery_bundle_candidate(item) for item in collected]
        instance = self._instance(task_id, instance_id)
        transition = None
        if instance["status"] == "RUNNING":
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                "WAITING_APPROVAL",
                CommandEnvelope(
                    idempotency_key=(
                        f"delivery-review-{instance_id}-"
                        f"{digest_json(sorted(item['bundle_id'] for item in candidates))[:24]}"
                    ),
                    actor_type="adapter",
                    actor_id=f"{instance['agent_type']}_adapter",
                    expected_revision=self.store.task.revision(task_id, task_id),
                ),
            )
            instance = next(
                item
                for item in transition["plan"]["instances"]
                if item["instance_id"] == instance_id
            )
        elif instance["status"] != "WAITING_APPROVAL":
            raise HarnessError(
                "INVALID_STATE_TRANSITION",
                "A delivery review candidate requires a running or waiting instance.",
            )
        approvals = [
            self.approvals.ensure_delivery_review_approval(task_id, instance_id, candidate)
            for candidate in candidates
            if candidate["status"] == "PENDING_CONFIRMATION"
        ]
        return {
            "status": "PENDING_CONFIRMATION",
            "instance": deepcopy(instance),
            "candidates": deepcopy(candidates),
            "approvals": approvals,
            "approval": approvals[0] if approvals else None,
            "transition": transition,
            "observation": {
                "step_id": observation.step_id,
                "details": deepcopy(observation.details),
            },
        }

    def _publish_delivery_bundle(
        self,
        candidate: dict[str, Any],
        *,
        actor: dict[str, str],
        published_at: str,
        crash_hook: CrashHook | None,
    ) -> dict[str, Any]:
        task_id = candidate["task_id"]
        instance_id = candidate["instance_id"]
        plan = self._plan(task_id)
        card = next(
            item for item in plan["task_cards"] if item["instance_id"] == instance_id
        )
        expected_images = [
            item
            for item in card["expected_deliveries"]
            if item["required"] and item["kind"] == "image"
        ]
        if len(expected_images) != 1:
            raise HarnessError(
                "VALIDATION_ERROR", "A delivery bundle requires one final image contract."
            )
        image = self.assets.inspect_delivery(
            task_id,
            instance_id,
            source_relative_path=candidate["image"]["private_relative_path"],
            role=expected_images[0]["role"],
            description=f"Branch {candidate['branch_id']} final artwork.",
            expected_sha256=candidate["image"]["sha256"],
        )
        note = self.assets.inspect_delivery(
            task_id,
            instance_id,
            source_relative_path=candidate["design_note"]["private_relative_path"],
            role="design_note",
            description=f"Branch {candidate['branch_id']} Markdown design note.",
            expected_sha256=candidate["design_note"]["sha256"],
        )
        if (
            image["mime_type"] != candidate["image"]["mime_type"]
            or note["mime_type"] != "text/markdown"
            or image["size_bytes"] != candidate["image"]["size_bytes"]
            or note["size_bytes"] != candidate["design_note"]["size_bytes"]
        ):
            raise HarnessError(
                "ASSET_CORRUPTED", "A delivery bundle changed after candidate collection."
            )
        batch_identity = {
            "bundle_id": candidate["bundle_id"],
            "image": image["sha256"],
            "note": note["sha256"],
        }
        batch_id = f"batch_{digest_json(batch_identity)[:32]}"
        image_manifest = self.assets.publish_delivery(
            task_id,
            instance_id,
            source_relative_path=image["source_relative_path"],
            role=image["role"],
            description=image["description"],
            idempotency_key=f"bundle-{candidate['bundle_id']}-image",
            batch_id=batch_id,
            bundle_id=candidate["bundle_id"],
        )
        note_manifest = self.assets.publish_delivery(
            task_id,
            instance_id,
            source_relative_path=note["source_relative_path"],
            role=note["role"],
            description=note["description"],
            idempotency_key=f"bundle-{candidate['bundle_id']}-note",
            batch_id=batch_id,
            bundle_id=candidate["bundle_id"],
        )
        bundle_manifest = {
            "schema_version": "1.0",
            "bundle_id": candidate["bundle_id"],
            "task_id": task_id,
            "work_item_id": candidate["work_item_id"],
            "instance_id": instance_id,
            "task_card_revision": candidate["task_card_revision"],
            "branch_id": candidate["branch_id"],
            "checkpoint_id": candidate["checkpoint_id"],
            "publication_batch_id": batch_id,
            "image_asset": {
                "asset_id": image_manifest["asset_id"],
                "manifest_relpath": f"resources/manifests/{image_manifest['asset_id']}.json",
            },
            "design_note_asset": {
                "asset_id": note_manifest["asset_id"],
                "manifest_relpath": f"resources/manifests/{note_manifest['asset_id']}.json",
            },
            "actor": deepcopy(actor),
            "created_at": candidate["created_at"],
            "published_at": published_at,
        }
        self.store.contracts.validate("bundle-manifest", bundle_manifest)
        batch = self.assets.commit_publication_batch(
            task_id,
            instance_id,
            batch_id=batch_id,
            manifests=[image_manifest, note_manifest],
            bundle_manifest=bundle_manifest,
            crash_hook=crash_hook,
        )
        if self._required_delivery_selection(
            task_id, instance_id, [image_manifest, note_manifest]
        ) is None:
            raise HarnessError(
                "VALIDATION_ERROR", "The bundle image does not satisfy its TaskCard contract."
            )
        return {
            "batch": batch,
            "batch_id": batch_id,
            "manifests": [image_manifest, note_manifest],
            "bundle_manifest": bundle_manifest,
        }

    def _resume_delivery_review_approval(
        self, intent_path: Path, crash_hook: CrashHook | None
    ) -> dict[str, Any]:
        intent = read_json(intent_path)
        request = intent["request"]
        envelope = CommandEnvelope.model_validate(request["envelope"])
        approval_details = self.approvals.get_approval(request["approval_id"])
        approval_payload = approval_details["payload"]
        task_id = intent["task_id"]
        instance_id = intent["instance_id"]
        bundle_id = str(approval_payload.get("bundle_id", ""))
        candidates = self.list_delivery_bundle_candidates(task_id, instance_id=instance_id)
        candidate = next((item for item in candidates if item["bundle_id"] == bundle_id), None)
        if candidate is None:
            raise HarnessError("VALIDATION_ERROR", "The approved delivery bundle is missing.")
        actor = {"type": envelope.actor_type, "id": envelope.actor_id}
        if request["decision"] == "APPROVED":
            if request["action"] != "publish_bundle" or request["payload"]:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A delivery review approval only accepts the frozen bundle.",
                )
            if intent.get("publication") is None:
                publication = self._publish_delivery_bundle(
                    candidate,
                    actor=actor,
                    published_at=intent["prepared_at"],
                    crash_hook=crash_hook,
                )
                intent.update(state="PUBLICATION_COMMITTED", publication=publication)
                atomic_write_json(intent_path, intent)
                if crash_hook:
                    crash_hook("after_delivery_bundle_publication")
            candidate = self._decide_delivery_bundle_candidate(
                task_id,
                bundle_id,
                status="PUBLISHED",
                actor=actor,
                decided_at=intent["prepared_at"],
                publication_batch_id=intent["publication"]["batch_id"],
            )
        else:
            candidate = self._decide_delivery_bundle_candidate(
                task_id,
                bundle_id,
                status="REJECTED",
                actor=actor,
                decided_at=intent["prepared_at"],
                publication_batch_id=None,
            )
        if crash_hook:
            crash_hook("after_delivery_candidate_decision")
        resolution = self.approvals.commit_resolution(
            request["approval_id"], request["decision"], envelope
        )
        if crash_hook:
            crash_hook("after_delivery_approval_commit")
        self.approvals.handle_approval_notification(
            request["approval_id"],
            Actor(envelope.actor_type, envelope.actor_id),
            self._derived_id("handled", intent["operation_id"], request["approval_id"]),
        )
        current = self._instance(task_id, instance_id)
        transition = None
        if request["decision"] == "APPROVED" and current["status"] == "WAITING_APPROVAL":
            transition = self.commands.transition_instance(
                task_id,
                instance_id,
                "SUCCEEDED",
                CommandEnvelope(
                    idempotency_key=self._derived_id(
                        "bundle-success", intent["operation_id"], bundle_id
                    ),
                    actor_type=envelope.actor_type,
                    actor_id=envelope.actor_id,
                    expected_revision=self.store.task.revision(task_id, task_id),
                ),
            )
            self._ensure_success_notifications(task_id, instance_id, transition)
        elif request["decision"] == "REJECTED" and current["status"] == "WAITING_APPROVAL":
            all_candidates = self.list_delivery_bundle_candidates(
                task_id, instance_id=instance_id
            )
            if not any(item["status"] == "PENDING_CONFIRMATION" for item in all_candidates):
                target = (
                    "SUCCEEDED"
                    if any(item["status"] == "PUBLISHED" for item in all_candidates)
                    else "FAILED"
                )
                transition = self.commands.transition_instance(
                    task_id,
                    instance_id,
                    target,
                    CommandEnvelope(
                        idempotency_key=self._derived_id(
                            "bundle-decision", intent["operation_id"], bundle_id
                        ),
                        actor_type=envelope.actor_type,
                        actor_id=envelope.actor_id,
                        expected_revision=self.store.task.revision(task_id, task_id),
                    ),
                )
                if target == "SUCCEEDED":
                    self._ensure_success_notifications(task_id, instance_id, transition)
        if crash_hook:
            crash_hook("after_delivery_instance_transition")
        current = self._instance(task_id, instance_id)
        result = {
            "approval": resolution["approval"],
            "approval_revision": resolution["approval_revision"],
            "candidate": candidate,
            "bundle_manifest": (
                None
                if intent.get("publication") is None
                else intent["publication"]["bundle_manifest"]
            ),
            "instance": deepcopy(current),
            "transition": transition,
            "advance": None,
        }
        intent.update(state="COMMITTED", committed_at=utc_now(), result=result)
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
        instance = self._instance(task_id, instance_id)
        if instance["agent_type"] not in {"image", "ppt"}:
            return
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
