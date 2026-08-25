"""Harness-owned instance settings proposals and safe-point application sagas."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from ..adapters import AdapterRegistry
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import atomic_write_json, digest_json, read_json
from ..storage.config_revision_io import validate_public_config_tree
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .agent_config_materialization import (
    LIBRARY_RELEASE_FIELDS,
    MODEL_STATES,
    RUNTIME_SETTING_FIELDS,
    ImageAgentConfigMaterializer,
)
from .runtime_config_observability import RuntimeConfigObservability
from .runtime_config_state import (
    application_task_lock_path,
    instance_has_process_evidence,
    is_unstarted_image_instance,
)
from .task_config import TaskConfigService
from .work_item_projections import logical_work_items

CrashHook = Callable[[str], None]
_TERMINAL_SAGA_STATES = frozenset({"APPLIED", "FAILED"})
_ACTIVE_INSTANCE_STATES = frozenset({"STARTING", "RUNNING", "WAITING_APPROVAL"})
_LOCKED_INSTANCE_STATES = frozenset(
    {"SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED", "ARCHIVED", "CRASHED"}
)
_FIELD_CONSUMERS = {
    "question_preference": "future_clarification_questions",
    "max_auto_questions": "future_clarification_questions",
    "clarification_total_budget": "future_clarification_questions",
    "category_constraint": "future_category_library_boundary",
    "style_direction": "future_style_library_boundary",
    "candidate_concurrency": "future_candidate_generation",
    "default_output_size": "future_image_generation",
    "response_format": "future_image_generation",
    "watermark": "future_image_generation",
    "self_check": "future_self_check_iterations",
    "advanced_model_overrides": "future_model_calls",
}


class InstanceRuntimeSettingsService:
    """Expose safe settings while preserving immutable execution history."""

    def __init__(
        self,
        store: FileStateStore,
        task_config: TaskConfigService,
        materializer: ImageAgentConfigMaterializer,
        adapters: AdapterRegistry,
        observability: RuntimeConfigObservability,
    ) -> None:
        self.store = store
        self.task_config = task_config
        self.materializer = materializer
        self.adapters = adapters
        self.observability = observability
        self.saga_root = store.layout.control_root / "runtime-config-sagas"
        self.saga_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def get(self, task_id: str, instance_id: str) -> dict[str, Any]:
        instance = self._image_instance(task_id, instance_id)
        current = self.materializer.revisions.read_current(task_id, instance_id)
        if current is None:
            self.materializer.materialize(task_id, instance_id)
            current = self.materializer.revisions.read_current(task_id, instance_id)
        assert current is not None
        current_task_revision = self.task_config.get_current(task_id)["revision"]
        task_revision = self.task_config.revisions.read_revision(
            task_id,
            current["manifest"]["task_config_revision_id"],
        )
        baseline_is_current = (
            task_revision["revision_id"] == current_task_revision["revision_id"]
        )
        editable = (
            not bool(current.get("legacy"))
            and baseline_is_current
            and self._pending_saga(task_id, instance_id, include_sync=True) is None
            and self._terminal_saga(task_id, instance_id, state="FAILED") is None
        )
        inherited = self.materializer.effective_runtime(task_revision, {})
        effective = self.materializer.effective_runtime(task_revision, {})
        for field, value in current["manifest"]["effective_runtime"].items():
            if field in {*LIBRARY_RELEASE_FIELDS, "self_check"}:
                effective[field].update(deepcopy(value))
            else:
                effective[field] = deepcopy(value)
        inherited_models = self._effective_model_ids(task_revision, {})
        effective_models = self._effective_model_ids(
            task_revision, current["manifest"]["overrides"]
        )
        values: dict[str, Any] = {}
        for field in RUNTIME_SETTING_FIELDS:
            if field == "advanced_model_overrides":
                inherited_value = inherited_models
                effective_value = effective_models
            else:
                inherited_value = inherited[field]
                effective_value = effective[field]
            overridden = field in current["manifest"]["overrides"]
            values[field] = {
                "inherited": deepcopy(inherited_value),
                "effective": deepcopy(effective_value),
                "overridden": overridden,
                "source": "instance" if overridden else "task_baseline",
                "explicit": deepcopy(current["manifest"]["overrides"].get(field)),
            }
        return {
            "schema_version": "2.0",
            "scope": {
                "task_id": task_id,
                "work_item_id": self._work_item_ids(task_id)[instance_id],
                "instance_id": instance_id,
            },
            "revision": {
                "current": int(current["state"]["revision"]),
                "revision_id": current["manifest"]["revision_id"],
                "task_config_revision_id": current["manifest"]["task_config_revision_id"],
                "config_hash": current["manifest"]["config_hash"],
                "pending_revision_id": current["state"].get("pending_revision_id"),
            },
            "values": values,
            "editable": editable,
            "editable_schema": self._editable_schema() if editable else {},
            "model_options": (
                self.materializer.model_options(current_task_revision) if editable else {}
            ),
            "workflow_boundary": self._workflow_boundary(task_id, instance),
            "sync_candidates": self._sync_candidates(task_id, instance_id),
            "pending_application": self._pending_projection(task_id, instance_id),
        }

    def propose(
        self,
        task_id: str,
        instance_id: str,
        *,
        base_revision: int,
        patch: dict[str, Any],
        sync_unstarted_image_work_items: bool,
        expected_sync_instance_ids: list[str],
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        self._require_settings_actor(envelope)
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "base_revision": base_revision,
            "patch": deepcopy(patch),
            "sync_unstarted_image_work_items": sync_unstarted_image_work_items,
            "expected_sync_instance_ids": sorted(expected_sync_instance_ids),
            "envelope": envelope.model_dump(mode="json"),
        }
        request_hash = digest_json(request)
        proposal_id = self._proposal_id(task_id, instance_id, envelope.idempotency_key)
        observed_instance = self._image_instance(task_id, instance_id)
        path = self._proposal_path(task_id, instance_id, proposal_id)
        observed_boundary = None
        if not path.exists():
            observed_boundary = self._workflow_boundary(task_id, observed_instance)
        with FileLock(
            application_task_lock_path(self.store, task_id),
            self.store.lock_timeout_seconds,
        ):
            if path.exists():
                existing = read_json(path)
                if existing.get("request_hash") != request_hash:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The settings proposal idempotency key was reused for another request.",
                    )
                return self._proposal_response(existing)
            self._validate_task_revision(task_id, envelope.expected_revision)
            if self._pending_saga(task_id, instance_id, include_sync=True) is not None:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The instance already has an unfinished runtime settings operation.",
                )
            if self._terminal_saga(task_id, instance_id, state="FAILED") is not None:
                raise HarnessError(
                    "INSTANCE_CONFIG_LOCKED",
                    "A failed settings saga must be repaired before another proposal.",
                )
            instance = self._image_instance(task_id, instance_id)
            current = self.materializer.revisions.read_current(task_id, instance_id)
            if current is None:
                self.materializer.materialize(task_id, instance_id)
                current = self.materializer.revisions.read_current(task_id, instance_id)
            assert current is not None
            if current.get("legacy"):
                raise HarnessError(
                    "INSTANCE_CONFIG_LOCKED",
                    "Legacy Image instances remain read-only until explicitly migrated.",
                )
            if int(current["state"]["revision"]) != base_revision:
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The runtime settings base revision changed before preview.",
                    {
                        "expected_revision": base_revision,
                        "actual_revision": int(current["state"]["revision"]),
                    },
                )
            self._validate_patch_shape(patch)
            overrides = self.materializer.merge_overrides(
                current["manifest"]["overrides"], patch
            )
            task_revision = self.task_config.get_current(task_id)["revision"]
            if (
                current["manifest"]["task_config_revision_id"]
                != task_revision["revision_id"]
            ):
                raise HarnessError(
                    "SETTINGS_REVISION_CONFLICT",
                    "The instance baseline is awaiting the task configuration rebase.",
                )
            overrides = self.materializer.validate_overrides(
                task_revision,
                overrides,
                require_current_approval=True,
            )
            candidates = self._sync_candidates(task_id, instance_id)
            candidate_ids = sorted(item["instance_id"] for item in candidates)
            expected = sorted(expected_sync_instance_ids)
            if len(expected) != len(set(expected)):
                raise HarnessError("VALIDATION_ERROR", "Synchronization targets must be unique.")
            if sync_unstarted_image_work_items:
                if expected != candidate_ids:
                    self.observability.increment("sync_scope_changes")
                    raise HarnessError(
                        "SYNC_SCOPE_CHANGED",
                        "The unstarted Image synchronization scope changed before preview.",
                        {"expected_instance_ids": expected, "actual_instance_ids": candidate_ids},
                    )
                sync_ids = candidate_ids
            elif expected:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "Synchronization targets require the synchronization option.",
                )
            else:
                sync_ids = []
            sync_bases = {
                target_id: self._config_identity(task_id, target_id)
                for target_id in sync_ids
            }
            mode = self._apply_mode(task_id, instance)
            boundary = self._proposal_boundary(mode, observed_boundary, instance)
            effective = self.materializer.effective_runtime(task_revision, overrides)
            effective_models = self._effective_model_ids(task_revision, overrides)
            proposal = {
                "schema_version": "1.0",
                "proposal_id": proposal_id,
                "task_id": task_id,
                "instance_id": instance_id,
                "request_hash": request_hash,
                "base_revision": base_revision,
                "base_revision_id": current["manifest"]["revision_id"],
                "base_config_hash": current["manifest"]["config_hash"],
                "task_config_revision_id": task_revision["revision_id"],
                "patch": deepcopy(patch),
                "overrides": deepcopy(overrides),
                "effective_runtime": effective,
                "effective_model_ids": effective_models,
                "diff": self._diff(
                    current,
                    self._effective_model_ids(
                        task_revision, current["manifest"]["overrides"]
                    ),
                    effective,
                    effective_models,
                ),
                "apply_mode": mode,
                "workflow_boundary": boundary,
                "sync_instance_ids": sync_ids,
                "sync_requested": sync_unstarted_image_work_items,
                "sync_bases": sync_bases,
                "created_by": {"actor_type": envelope.actor_type, "actor_id": envelope.actor_id},
                "created_at": utc_now(),
                "status": "DRAFT",
                "confirmed_at": None,
                "confirm_idempotency_key": None,
            }
            self._write_proposal(path, proposal)
            self.observability.event(
                task_id,
                "CONFIG_PROPOSAL_CREATED",
                actor=proposal["created_by"],
                fields={
                    "instance_id": instance_id,
                    "proposal_id": proposal_id,
                    "old_hash": current["manifest"]["config_hash"],
                    "changed_fields": sorted(item["field"] for item in proposal["diff"]),
                    "result": "DRAFT",
                },
            )
            return self._proposal_response(proposal)

    def confirm(
        self,
        task_id: str,
        instance_id: str,
        proposal_id: str,
        *,
        envelope: CommandEnvelope,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any]:
        self._require_settings_actor(envelope)
        validate_identifier(proposal_id, "proposal_id")
        confirm_request_hash = digest_json(
            {
                "task_id": task_id,
                "instance_id": instance_id,
                "proposal_id": proposal_id,
                "envelope": envelope.model_dump(mode="json"),
            }
        )
        with FileLock(
            application_task_lock_path(self.store, task_id),
            self.store.lock_timeout_seconds,
        ):
            proposal_path = self._proposal_path(task_id, instance_id, proposal_id)
            if not proposal_path.is_file():
                raise HarnessError("INSTANCE_NOT_FOUND", "The settings proposal does not exist.")
            proposal = read_json(proposal_path)
            saga_path = self._saga_path(proposal_id)
            if proposal.get("confirm_idempotency_key") is not None:
                if (
                    proposal["confirm_idempotency_key"] != envelope.idempotency_key
                    or proposal.get("confirm_request_hash") != confirm_request_hash
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The settings confirmation idempotency key was reused for another request.",
                    )
                if not saga_path.is_file():
                    raise HarnessError(
                        "CONFIG_INTEGRITY_FAILED",
                        "The confirmed settings proposal lost its application saga.",
                    )
                saga = read_json(saga_path)
                self._validate_confirmation_saga(
                    saga,
                    task_id=task_id,
                    instance_id=instance_id,
                    proposal_id=proposal_id,
                    confirm_request_hash=confirm_request_hash,
                    idempotency_key=envelope.idempotency_key,
                )
                if saga["state"] == "INTENT_PERSISTED":
                    saga.update({"state": "CONFIRMED", "updated_at": utc_now()})
                    self._write_saga(saga)
                replay = self._saga_response(saga)
            elif saga_path.exists():
                saga = read_json(saga_path)
                self._validate_confirmation_saga(
                    saga,
                    task_id=task_id,
                    instance_id=instance_id,
                    proposal_id=proposal_id,
                    confirm_request_hash=confirm_request_hash,
                    idempotency_key=envelope.idempotency_key,
                )
                proposal.update(
                    {
                        "status": "CONFIRMED",
                        "confirmed_at": saga["confirmed_at"],
                        "confirm_idempotency_key": envelope.idempotency_key,
                        "confirm_request_hash": confirm_request_hash,
                    }
                )
                self._write_proposal(proposal_path, proposal)
                self.observability.increment("proposals_confirmed")
                self.observability.event(
                    task_id,
                    "CONFIG_PROPOSAL_CONFIRMED",
                    actor=saga["actor"],
                    fields={
                        "instance_id": instance_id,
                        "proposal_id": proposal_id,
                        "revision_id": saga["source"]["revision_id"],
                        "result": "CONFIRMED",
                    },
                )
                saga.update({"state": "CONFIRMED", "updated_at": utc_now()})
                self._write_saga(saga)
                self._resume_sync_targets_locked(saga, crash_hook)
                if saga["mode"] == "before_start":
                    self._resume_local_source_locked(saga, crash_hook)
                    replay = self._complete_local_saga(saga)
                else:
                    replay = self._saga_response(saga)
            else:
                self._validate_task_revision(task_id, envelope.expected_revision)
                instance = self._image_instance(task_id, instance_id)
                current = self.materializer.revisions.read_current(task_id, instance_id)
                if current is None or current.get("legacy"):
                    raise HarnessError(
                        "INSTANCE_CONFIG_LOCKED",
                        "The instance runtime configuration is not editable.",
                    )
                self._validate_base(proposal, current)
                self._validate_sync_scope(task_id, instance_id, proposal)
                task_revision = self.task_config.get_current(task_id)["revision"]
                if proposal["task_config_revision_id"] != task_revision["revision_id"]:
                    raise HarnessError(
                        "SETTINGS_REVISION_CONFLICT",
                        "The task configuration baseline changed after preview.",
                    )
                self.materializer.validate_overrides(
                    task_revision,
                    proposal["overrides"],
                    require_current_approval=True,
                )
                mode = self._apply_mode(task_id, instance)
                if mode != proposal["apply_mode"]:
                    raise HarnessError(
                        "SETTINGS_REVISION_CONFLICT",
                        "The instance start boundary changed after preview.",
                    )
                now = utc_now()
                source_revision_id = self._next_revision_id(current)
                saga = {
                    "schema_version": "1.0",
                    "proposal_id": proposal_id,
                    "task_id": task_id,
                    "instance_id": instance_id,
                    "mode": mode,
                    "state": "INTENT_PERSISTED",
                    "actor": {"actor_type": envelope.actor_type, "actor_id": envelope.actor_id},
                    "confirm_idempotency_key": envelope.idempotency_key,
                    "confirm_request_hash": confirm_request_hash,
                    "confirm_envelope": envelope.model_dump(mode="json"),
                    "confirmed_at": now,
                    "updated_at": now,
                    "source": {
                        "instance_id": instance_id,
                        "base": self._identity_from_current(current),
                        "revision_id": source_revision_id,
                        "overrides": deepcopy(proposal["overrides"]),
                        "bundle": None,
                        "state": "PENDING",
                    },
                    "sync_targets": [
                        {
                            "instance_id": target_id,
                            "base": deepcopy(proposal["sync_bases"][target_id]),
                            "revision_id": self._next_revision_id(
                                self.materializer.revisions.read_current(task_id, target_id)
                            ),
                            "overrides": deepcopy(proposal["overrides"]),
                            "bundle": None,
                            "state": "PENDING",
                        }
                        for target_id in proposal["sync_instance_ids"]
                    ],
                    "from_checkpoint": None,
                    "effective_from_state": None,
                    "receipt": None,
                    "last_error": None,
                }
                self._write_saga(saga)
                if crash_hook:
                    crash_hook("after_config_saga_persisted")
                proposal.update(
                    {
                        "status": "CONFIRMED",
                        "confirmed_at": now,
                        "confirm_idempotency_key": envelope.idempotency_key,
                        "confirm_request_hash": confirm_request_hash,
                    }
                )
                self._write_proposal(proposal_path, proposal)
                self.observability.increment("proposals_confirmed")
                self.observability.event(
                    task_id,
                    "CONFIG_PROPOSAL_CONFIRMED",
                    actor=saga["actor"],
                    fields={
                        "instance_id": instance_id,
                        "proposal_id": proposal_id,
                        "revision_id": source_revision_id,
                        "result": "CONFIRMED",
                    },
                )
                saga.update({"state": "CONFIRMED", "updated_at": utc_now()})
                self._write_saga(saga)
                if crash_hook:
                    crash_hook("after_config_saga_intent")
                self._resume_sync_targets_locked(saga, crash_hook)
                if mode == "before_start":
                    self._resume_local_source_locked(saga, crash_hook)
                    replay = self._complete_local_saga(saga)
                else:
                    replay = self._saga_response(saga)
        if replay["status"] in {"APPLIED_BEFORE_START", "APPLIED_ON_BRANCH", "FAILED"}:
            if replay["status"] == "FAILED":
                self._raise_saga_failure(saga)
            return replay
        return self.apply_pending_if_safe(task_id, instance_id, crash_hook=crash_hook) or replay

    def apply_pending_if_safe(
        self,
        task_id: str,
        instance_id: str,
        *,
        crash_hook: CrashHook | None = None,
    ) -> dict[str, Any] | None:
        saga = self._pending_saga(task_id, instance_id)
        if saga is None:
            failed = self._terminal_saga(task_id, instance_id, state="FAILED")
            if failed is not None:
                raise HarnessError(
                    str(
                        (failed.get("last_error") or {}).get(
                            "code", "CONFIG_INTEGRITY_FAILED"
                        )
                    ),
                    str(
                        (failed.get("last_error") or {}).get(
                            "message", "Runtime configuration application failed."
                        )
                    ),
                    {"proposal_id": failed["proposal_id"]},
                )
            return None
        if saga["state"] == "INTENT_PERSISTED":
            return self._saga_response(saga)
        if saga["mode"] == "before_start":
            with FileLock(
                application_task_lock_path(self.store, task_id),
                self.store.lock_timeout_seconds,
            ):
                self._resume_sync_targets_locked(saga, crash_hook)
                self._resume_local_source_locked(saga, crash_hook)
                return self._complete_local_saga(saga)
        gate = FileLock(
            self._instance_gate_path(task_id, instance_id),
            self.store.lock_timeout_seconds,
        )
        with gate:
            saga = read_json(self._saga_path(saga["proposal_id"]))
            if saga["state"] == "APPLIED":
                return self._saga_response(saga)
            if saga["state"] == "FAILED":
                return self._saga_response(saga)
            boundary = self._workflow_boundary(
                task_id, self._image_instance(task_id, instance_id)
            )
            if boundary["reason"] == "INSTANCE_CONFIG_LOCKED":
                return self._fail_saga(
                    saga,
                    HarnessError(
                        "INSTANCE_CONFIG_LOCKED",
                        "The instance completed before its pending configuration could apply.",
                    ),
                )
            if not boundary["safe_now"]:
                return self._wait_at_safe_point(saga, boundary)
            if saga["source"]["bundle"] is None:
                bundle = self.materializer.build_revision(
                    task_id,
                    instance_id,
                    overrides=saga["source"]["overrides"],
                    created_by={
                        "type": saga["actor"]["actor_type"],
                        "id": saga["actor"]["actor_id"],
                    },
                    apply_mode="safe_checkpoint_branch",
                    apply_status="CONFIRMED",
                    confirmed_at=saga["confirmed_at"],
                    effective_from_state=boundary["state"],
                    revision_id=saga["source"]["revision_id"],
                    require_current_approval=True,
                )
                saga["source"]["bundle"] = bundle
                saga["from_checkpoint"] = boundary["checkpoint_id"]
                saga["effective_from_state"] = boundary["state"]
                saga["updated_at"] = utc_now()
                self._write_saga(saga)
                if crash_hook:
                    crash_hook("after_safe_revision_prepared")
            bundle = saga["source"]["bundle"]
            self.materializer.publish_revision(bundle)
            current = self.materializer.revisions.read_current(task_id, instance_id)
            assert current is not None
            if current["state"]["current_revision_id"] != bundle["manifest"]["revision_id"]:
                self.materializer.revisions.set_pending(
                    task_id,
                    instance_id,
                    bundle["manifest"]["revision_id"],
                    expected_revision=int(current["state"]["revision"]),
                    updated_at=utc_now(),
                )
            if saga["state"] in {"CONFIRMED", "WAITING_SAFE_POINT"}:
                saga.update({"state": "MATERIALIZED", "updated_at": utc_now()})
                self._write_saga(saga)
                if crash_hook:
                    crash_hook("after_safe_revision_materialized")
            if saga.get("receipt") is None:
                adapter = self.adapters.get("image")
                apply = getattr(adapter, "apply_runtime_revision", None)
                if not callable(apply):
                    return self._fail_saga(
                        saga,
                        HarnessError(
                            "CONTROL_PLANE_NOT_READY",
                            "The Image Adapter does not support runtime revision application.",
                        ),
                    )
                apply_revision = cast(Callable[..., dict[str, Any]], apply)
                try:
                    receipt = apply_revision(
                        instance_id,
                        revision_id=bundle["manifest"]["revision_id"],
                        from_checkpoint=saga["from_checkpoint"],
                        expected_config_hash=bundle["manifest"]["config_hash"],
                        effective_from_state=saga["effective_from_state"],
                        idempotency_key=self._apply_idempotency_key(saga),
                    )
                    if not isinstance(receipt, dict):
                        raise HarnessError(
                            "CONFIG_INTEGRITY_FAILED",
                            "The Image Agent returned a malformed configuration receipt.",
                        )
                    if receipt.get("from_checkpoint") != saga["from_checkpoint"]:
                        raise HarnessError(
                            "CONFIG_INTEGRITY_FAILED",
                            "The Image Agent applied the revision from another checkpoint.",
                        )
                except HarnessError as exc:
                    if exc.code in {"SAFE_CHECKPOINT_UNAVAILABLE", "PROCESS_START_FAILED"}:
                        return self._wait_at_safe_point(
                            saga,
                            {**boundary, "reason": exc.code},
                            error=exc,
                        )
                    if exc.code == "CONFIG_INTEGRITY_FAILED":
                        self.observability.increment("config_hash_mismatches")
                    self.observability.increment("config_branch_create_failures")
                    return self._fail_saga(saga, exc)
                applied_at = utc_now()
                saga["receipt"] = {**deepcopy(receipt), "applied_at": applied_at}
                saga.update({"state": "CHILD_BRANCH_CREATED", "updated_at": applied_at})
                self._write_saga(saga)
                if crash_hook:
                    crash_hook("after_config_child_branch_created")
            applied_at = saga["receipt"]["applied_at"]
            current = self.materializer.revisions.read_current(task_id, instance_id)
            assert current is not None
            target_revision_id = bundle["manifest"]["revision_id"]
            if current["state"]["current_revision_id"] != target_revision_id:
                self.materializer.revisions.set_current(
                    task_id,
                    instance_id,
                    target_revision_id,
                    expected_revision=int(current["state"]["revision"]),
                    updated_at=applied_at,
                    applied_receipt=saga["receipt"],
                )
            saga.update({"state": "INSTANCE_POINTER_COMMITTED", "updated_at": utc_now()})
            self._write_saga(saga)
            if crash_hook:
                crash_hook("after_config_instance_pointer_committed")
            self._update_instance_config_revision(
                task_id,
                instance_id,
                target_revision_id,
                saga["actor"],
            )
            saga.update({"state": "APPLIED", "updated_at": utc_now(), "last_error": None})
            self._write_saga(saga)
            self._update_proposal_status(saga, "APPLIED")
            self.observability.increment("revisions_applied")
            self.observability.event(
                task_id,
                "CONFIG_REVISION_APPLIED",
                actor=saga["actor"],
                fields={
                    "instance_id": instance_id,
                    "proposal_id": saga["proposal_id"],
                    "revision_id": target_revision_id,
                    "config_hash": bundle["manifest"]["config_hash"],
                    "branch_id": saga["receipt"]["branch_id"],
                    "checkpoint_id": saga["receipt"]["checkpoint_id"],
                    "result": "APPLIED_ON_BRANCH",
                },
            )
            self._observe_latency(saga)
            return self._saga_response(saga)

    def ensure_before_start(self, task_id: str, instance_id: str) -> None:
        """Finish confirmed local copies before the process materializes config."""

        with FileLock(
            application_task_lock_path(self.store, task_id),
            self.store.lock_timeout_seconds,
        ):
            self.ensure_before_start_locked(task_id, instance_id)

    def ensure_before_start_locked(self, task_id: str, instance_id: str) -> None:
        """Apply pre-start revisions while the caller owns the task start lock."""

        for saga in self._sagas_for_instance(task_id, instance_id, include_sync=True):
            if saga["state"] in _TERMINAL_SAGA_STATES:
                continue
            if saga["state"] == "INTENT_PERSISTED":
                raise HarnessError(
                    "SAFE_CHECKPOINT_UNAVAILABLE",
                    "Recover the persisted settings confirmation before starting the instance.",
                    {"proposal_id": saga["proposal_id"]},
                )
            self._resume_sync_targets_locked(saga, None)
            if saga["instance_id"] == instance_id and saga["mode"] == "before_start":
                self._resume_local_source_locked(saga, None)
                self._complete_local_saga(saga)

    def recover(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.saga_root.glob("*.json")):
            saga = read_json(path)
            if saga["state"] in _TERMINAL_SAGA_STATES:
                continue
            try:
                if saga["state"] == "INTENT_PERSISTED":
                    result = self.confirm(
                        saga["task_id"],
                        saga["instance_id"],
                        saga["proposal_id"],
                        envelope=CommandEnvelope.model_validate(saga["confirm_envelope"]),
                    )
                else:
                    result = self.apply_pending_if_safe(
                        saga["task_id"], saga["instance_id"]
                    )
                results.append({"proposal_id": saga["proposal_id"], "result": result})
            except HarnessError as exc:
                results.append({"proposal_id": saga["proposal_id"], "error_code": exc.code})
        return results

    def metrics(self) -> dict[str, Any]:
        return self.observability.snapshot(self.saga_root)

    def assert_no_pending_advance(self, task_id: str, instance_id: str) -> None:
        """Close the proposal-confirm/Agent-advance race under the task lock."""

        saga = self._pending_saga(task_id, instance_id)
        if saga is not None:
            raise HarnessError(
                "SAFE_CHECKPOINT_UNAVAILABLE",
                "Apply the confirmed runtime configuration before advancing the Agent.",
                {"proposal_id": saga["proposal_id"]},
            )

    def _resume_sync_targets_locked(
        self, saga: dict[str, Any], crash_hook: CrashHook | None
    ) -> None:
        for target in saga["sync_targets"]:
            self._resume_local_target(saga, target, crash_hook)
        self._write_saga(saga)

    def _resume_local_source_locked(
        self, saga: dict[str, Any], crash_hook: CrashHook | None
    ) -> None:
        self._resume_local_target(saga, saga["source"], crash_hook)
        self._write_saga(saga)

    def _resume_local_target(
        self,
        saga: dict[str, Any],
        target: dict[str, Any],
        crash_hook: CrashHook | None,
    ) -> None:
        if target["state"] == "APPLIED":
            return
        task_id = saga["task_id"]
        instance_id = target["instance_id"]
        instance = self._image_instance(task_id, instance_id)
        current = self.materializer.revisions.read_current(task_id, instance_id)
        if current is None:
            self.materializer.materialize(task_id, instance_id)
            current = self.materializer.revisions.read_current(task_id, instance_id)
        assert current is not None
        if current["state"]["current_revision_id"] == target["revision_id"]:
            self._update_instance_config_revision(
                task_id,
                instance_id,
                target["revision_id"],
                saga["actor"],
            )
            target["state"] = "APPLIED"
            return
        if instance_has_process_evidence(
            self.store,
            task_id,
            instance_id,
            instance=instance,
        ):
            raise HarnessError(
                "SYNC_SCOPE_CHANGED",
                "A frozen settings target started before its local revision committed.",
                {"instance_id": instance_id},
            )
        self._validate_identity(target["base"], current)
        if target["bundle"] is None:
            target["bundle"] = self.materializer.build_revision(
                task_id,
                instance_id,
                overrides=target["overrides"],
                created_by={
                    "type": saga["actor"]["actor_type"],
                    "id": saga["actor"]["actor_id"],
                },
                apply_mode="before_start",
                apply_status="APPLIED",
                confirmed_at=saga["confirmed_at"],
                effective_from_state="initial",
                revision_id=target["revision_id"],
                require_current_approval=True,
            )
            target["state"] = "BUNDLE_READY"
            saga["updated_at"] = utc_now()
            self._write_saga(saga)
            if crash_hook:
                crash_hook(f"after_local_bundle_prepared:{instance_id}")
        self.materializer.publish_revision(target["bundle"])
        state = self.materializer.revisions.set_current(
            task_id,
            instance_id,
            target["revision_id"],
            expected_revision=int(current["state"]["revision"]),
            updated_at=utc_now(),
        )
        if crash_hook:
            crash_hook(f"after_local_pointer_committed:{instance_id}")
        self._update_instance_config_revision(
            task_id, instance_id, target["revision_id"], saga["actor"]
        )
        target.update({"state": "APPLIED", "state_revision": state["revision"]})
        saga["updated_at"] = utc_now()
        self._write_saga(saga)
        if crash_hook:
            crash_hook(f"after_local_revision_applied:{instance_id}")

    def _complete_local_saga(self, saga: dict[str, Any]) -> dict[str, Any]:
        if saga["source"]["state"] != "APPLIED" or any(
            item["state"] != "APPLIED" for item in saga["sync_targets"]
        ):
            return self._saga_response(saga)
        saga.update({"state": "APPLIED", "updated_at": utc_now(), "last_error": None})
        self._write_saga(saga)
        self._update_proposal_status(saga, "APPLIED")
        bundle = saga["source"]["bundle"]
        assert bundle is not None
        self.observability.increment("revisions_applied")
        self.observability.event(
            saga["task_id"],
            "CONFIG_REVISION_APPLIED",
            actor=saga["actor"],
            fields={
                "instance_id": saga["instance_id"],
                "proposal_id": saga["proposal_id"],
                "revision_id": saga["source"]["revision_id"],
                "config_hash": bundle["manifest"]["config_hash"],
                "result": "APPLIED_BEFORE_START",
            },
        )
        self._observe_latency(saga)
        return self._saga_response(saga)

    def _wait_at_safe_point(
        self,
        saga: dict[str, Any],
        boundary: dict[str, Any],
        *,
        error: HarnessError | None = None,
    ) -> dict[str, Any]:
        first_wait = saga["state"] != "WAITING_SAFE_POINT"
        saga.update(
            {
                "state": "WAITING_SAFE_POINT",
                "updated_at": utc_now(),
                "last_error": (
                    None
                    if error is None
                    else {"code": error.code, "message": error.message}
                ),
            }
        )
        self._write_saga(saga)
        self._update_proposal_status(saga, "WAITING_SAFE_POINT")
        if first_wait:
            self.observability.event(
                saga["task_id"],
                "CONFIG_WAITING_SAFE_POINT",
                actor=saga["actor"],
                fields={
                    "instance_id": saga["instance_id"],
                    "proposal_id": saga["proposal_id"],
                    "revision_id": saga["source"]["revision_id"],
                    "reason": str(boundary.get("reason") or "ACTIVE_JOB"),
                    "result": "WAITING_SAFE_POINT",
                },
            )
        return self._saga_response(saga)

    def _fail_saga(self, saga: dict[str, Any], error: HarnessError) -> NoReturn:
        saga.update(
            {
                "state": "FAILED",
                "updated_at": utc_now(),
                "last_error": {"code": error.code, "message": error.message},
            }
        )
        self._write_saga(saga)
        self._update_proposal_status(saga, "FAILED")
        self.observability.event(
            saga["task_id"],
            "CONFIG_REVISION_FAILED",
            actor=saga["actor"],
            fields={
                "instance_id": saga["instance_id"],
                "proposal_id": saga["proposal_id"],
                "revision_id": saga["source"]["revision_id"],
                "error_code": error.code,
                "result": "FAILED",
            },
        )
        raise error

    @staticmethod
    def _raise_saga_failure(saga: dict[str, Any]) -> NoReturn:
        error = saga.get("last_error") or {}
        raise HarnessError(
            str(error.get("code") or "CONFIG_INTEGRITY_FAILED"),
            str(error.get("message") or "Runtime configuration application failed."),
            {"proposal_id": saga["proposal_id"]},
        )

    def _validate_sync_scope(
        self, task_id: str, instance_id: str, proposal: dict[str, Any]
    ) -> None:
        if not proposal.get("sync_requested", bool(proposal["sync_instance_ids"])):
            return
        actual_ids = sorted(
            item["instance_id"] for item in self._sync_candidates(task_id, instance_id)
        )
        expected_ids = sorted(proposal["sync_instance_ids"])
        if actual_ids != expected_ids:
            self.observability.increment("sync_scope_changes")
            raise HarnessError(
                "SYNC_SCOPE_CHANGED",
                "The unstarted Image synchronization scope changed before confirmation.",
                {"expected_instance_ids": expected_ids, "actual_instance_ids": actual_ids},
            )
        for target_id in expected_ids:
            current = self.materializer.revisions.read_current(task_id, target_id)
            if current is None:
                self.materializer.materialize(task_id, target_id)
                current = self.materializer.revisions.read_current(task_id, target_id)
            assert current is not None
            self._validate_identity(proposal["sync_bases"][target_id], current)

    def _sync_candidates(self, task_id: str, source_instance_id: str) -> list[dict[str, Any]]:
        plan = self.store.plan.get(task_id, task_id)
        if plan is None:
            return []
        work_item_ids = self._work_item_ids(task_id, plan=plan)
        candidates = []
        for instance in plan["instances"]:
            if instance["instance_id"] == source_instance_id:
                continue
            if is_unstarted_image_instance(self.store, task_id, instance):
                candidates.append(
                    {
                        "instance_id": instance["instance_id"],
                        "work_item_id": work_item_ids[instance["instance_id"]],
                    }
                )
        return sorted(candidates, key=lambda item: item["instance_id"])

    def _work_item_ids(
        self, task_id: str, *, plan: dict[str, Any] | None = None
    ) -> dict[str, str]:
        selected = self.store.plan.get(task_id, task_id) if plan is None else plan
        if selected is None:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "Runtime settings require a saved WorkItem plan.",
            )
        stages = sorted(selected["stages"], key=lambda item: item["position"])
        mapping: dict[str, str] = {}
        for item in logical_work_items(self.store, task_id, selected, stages):
            for instance_id in item["instance_ids"]:
                if instance_id in mapping:
                    raise HarnessError(
                        "CONFIG_INTEGRITY_FAILED",
                        "An Image instance resolves to multiple WorkItems.",
                        {"instance_id": instance_id},
                    )
                mapping[instance_id] = item["work_item_id"]
        planned_ids = {item["instance_id"] for item in selected["instances"]}
        if set(mapping) != planned_ids:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The WorkItem projection does not cover every planned instance.",
            )
        return mapping

    def _workflow_boundary(
        self, task_id: str, instance: dict[str, Any]
    ) -> dict[str, Any]:
        if is_unstarted_image_instance(self.store, task_id, instance):
            return {
                "state": "initial",
                "checkpoint_id": None,
                "safe_now": True,
                "reason": None,
            }
        if instance["status"] in _LOCKED_INSTANCE_STATES:
            return {
                "state": instance["status"],
                "checkpoint_id": None,
                "safe_now": False,
                "reason": "INSTANCE_CONFIG_LOCKED",
            }
        if instance["status"] not in _ACTIVE_INSTANCE_STATES:
            return {
                "state": instance["status"],
                "checkpoint_id": None,
                "safe_now": False,
                "reason": "SAFE_CHECKPOINT_UNAVAILABLE",
            }
        observation = self.adapters.get("image").get_status(instance["instance_id"])
        if observation.status not in _ACTIVE_INSTANCE_STATES:
            return {
                "state": observation.step_id or instance["status"],
                "checkpoint_id": None,
                "safe_now": False,
                "reason": "SAFE_CHECKPOINT_UNAVAILABLE",
            }
        boundary = observation.details.get("workflow_boundary")
        if not isinstance(boundary, dict):
            return {
                "state": observation.step_id or "running",
                "checkpoint_id": None,
                "safe_now": False,
                "reason": "SAFE_CHECKPOINT_UNAVAILABLE",
            }
        state = boundary.get("state")
        checkpoint_id = boundary.get("checkpoint_id")
        safe_now = boundary.get("safe_now")
        reason = boundary.get("reason")
        identifiers_valid = True
        try:
            if isinstance(state, str):
                validate_identifier(state, "workflow_state")
            if isinstance(checkpoint_id, str):
                validate_identifier(checkpoint_id, "checkpoint_id")
        except HarnessError:
            identifiers_valid = False
        valid = (
            identifiers_valid
            and
            isinstance(state, str)
            and bool(state)
            and type(safe_now) is bool
            and (checkpoint_id is None or isinstance(checkpoint_id, str))
            and (reason is None or isinstance(reason, str))
            and (not safe_now or bool(checkpoint_id))
            and (not safe_now or reason is None)
        )
        if not valid:
            return {
                "state": observation.step_id or "running",
                "checkpoint_id": None,
                "safe_now": False,
                "reason": "SAFE_CHECKPOINT_UNAVAILABLE",
            }
        return {
            "state": state,
            "checkpoint_id": checkpoint_id,
            "safe_now": safe_now,
            "reason": reason or (None if safe_now else "SAFE_CHECKPOINT_UNAVAILABLE"),
        }

    def _apply_mode(self, task_id: str, instance: dict[str, Any]) -> str:
        if is_unstarted_image_instance(self.store, task_id, instance):
            return "before_start"
        if instance["status"] in _LOCKED_INSTANCE_STATES:
            raise HarnessError(
                "INSTANCE_CONFIG_LOCKED",
                "The instance has no future execution boundary for runtime settings.",
            )
        return "safe_checkpoint_branch"

    @staticmethod
    def _proposal_boundary(
        mode: str,
        observed: dict[str, Any] | None,
        instance: dict[str, Any],
    ) -> dict[str, Any]:
        if mode == "before_start":
            return {
                "state": "initial",
                "checkpoint_id": None,
                "safe_now": True,
                "reason": None,
            }
        if observed is not None and observed["state"] != "initial":
            return deepcopy(observed)
        return {
            "state": instance["status"],
            "checkpoint_id": None,
            "safe_now": False,
            "reason": "SAFE_CHECKPOINT_UNAVAILABLE",
        }

    def _image_instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance.get("task_id") != task_id:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        if instance.get("agent_type") != "image":
            raise HarnessError(
                "FIELD_NOT_EDITABLE",
                "Runtime settings are only available for Image instances.",
            )
        return deepcopy(instance)

    def _config_identity(self, task_id: str, instance_id: str) -> dict[str, Any]:
        current = self.materializer.revisions.read_current(task_id, instance_id)
        if current is None:
            self.materializer.materialize(task_id, instance_id)
            current = self.materializer.revisions.read_current(task_id, instance_id)
        assert current is not None
        return self._identity_from_current(current)

    @staticmethod
    def _identity_from_current(current: dict[str, Any]) -> dict[str, Any]:
        return {
            "state_revision": int(current["state"]["revision"]),
            "revision_id": current["manifest"]["revision_id"],
            "config_hash": current["manifest"]["config_hash"],
        }

    @staticmethod
    def _validate_identity(expected: dict[str, Any], current: dict[str, Any]) -> None:
        actual = InstanceRuntimeSettingsService._identity_from_current(current)
        if expected != actual:
            raise HarnessError(
                "SETTINGS_REVISION_CONFLICT",
                "An instance runtime configuration changed after preview.",
                {
                    "expected_revision": expected["state_revision"],
                    "actual_revision": actual["state_revision"],
                },
            )

    @staticmethod
    def _validate_base(proposal: dict[str, Any], current: dict[str, Any]) -> None:
        if (
            int(current["state"]["revision"]) != proposal["base_revision"]
            or current["manifest"]["revision_id"] != proposal["base_revision_id"]
            or current["manifest"]["config_hash"] != proposal["base_config_hash"]
        ):
            raise HarnessError(
                "SETTINGS_REVISION_CONFLICT",
                "The instance runtime configuration changed after preview.",
                {
                    "expected_revision": proposal["base_revision"],
                    "actual_revision": int(current["state"]["revision"]),
                },
            )

    @staticmethod
    def _next_revision_id(current: dict[str, Any] | None) -> str:
        sequence = (
            1
            if current is None
            else int(current["manifest"]["revision_id"].rsplit("r", 1)[1]) + 1
        )
        return f"cfg-inst-r{sequence:06d}"

    @staticmethod
    def _effective_model_ids(
        task_revision: dict[str, Any], overrides: dict[str, Any]
    ) -> dict[str, str]:
        baseline = task_revision["runtime"]["image_agent"]["advanced_model_overrides"]
        selected = {
            **{name: value for name, value in baseline.items() if value is not None},
            **(overrides.get("advanced_model_overrides") or {}),
        }
        defaults = task_revision["runtime"]["models"]
        routes = {
            "intake_clarify": "text_reasoning",
            "confirmation_build": "text_reasoning",
            "initial_candidate_generation": "image_generation",
            "self_check_inspection": "vision_understanding",
            "self_check_rework": "image_generation",
            "human_prompt_rework": "image_generation",
        }
        return {state: selected.get(state) or defaults[route] for state, route in routes.items()}

    @staticmethod
    def _diff(
        current: dict[str, Any],
        current_model_ids: dict[str, str],
        effective: dict[str, Any],
        effective_models: dict[str, str],
    ) -> list[dict[str, Any]]:
        prior = current["manifest"]["effective_runtime"]
        changes = []
        for field in RUNTIME_SETTING_FIELDS:
            before = (
                current_model_ids
                if field == "advanced_model_overrides"
                else prior.get(field, {"release": "auto"})
            )
            after = effective_models if field == "advanced_model_overrides" else effective[field]
            if before == after:
                continue
            diff_field = field
            if field in LIBRARY_RELEASE_FIELDS:
                diff_field = f"{field}.release"
                before = before["release"]
                after = after["release"]
            changes.append(
                {
                    "field": diff_field,
                    "before": deepcopy(before),
                    "after": deepcopy(after),
                    "consumer_state": _FIELD_CONSUMERS[field],
                    "history_effect": "future_only",
                    "message": (
                        "Completed stages are not rerun; this value is consumed only by "
                        "later steps or an explicit future rerun."
                    ),
                }
            )
        return changes

    @staticmethod
    def _editable_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "question_preference": {
                    "type": ["string", "null"],
                    "enum": ["proactive", "blocking_only", None],
                },
                "max_auto_questions": {"type": ["integer", "null"], "minimum": 0, "maximum": 10},
                "clarification_total_budget": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "maximum": 100,
                },
                "category_constraint": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "release": {
                            "type": ["string", "null"],
                            "enum": ["auto", "manual", "off", None],
                        }
                    },
                },
                "style_direction": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "release": {
                            "type": ["string", "null"],
                            "enum": ["auto", "manual", "off", None],
                        }
                    },
                },
                "candidate_concurrency": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                "default_output_size": {
                    "type": ["string", "null"],
                    "pattern": r"^(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)$",
                },
                "response_format": {"type": ["string", "null"], "enum": ["url", "b64_json", None]},
                "watermark": {"type": ["boolean", "null"]},
                "self_check": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        "termination": {
                            "type": ["string", "null"],
                            "enum": ["fix", "solo", None],
                        },
                        "fixed_rounds": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "maximum": 20,
                        },
                        "max_rounds": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "maximum": 50,
                        },
                        "stop_early_on_pass": {"type": ["boolean", "null"]},
                    },
                },
                "advanced_model_overrides": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "properties": {
                        state: {"type": ["string", "null"]} for state in MODEL_STATES
                    },
                },
            },
        }

    @staticmethod
    def _validate_patch_shape(patch: dict[str, Any]) -> None:
        unknown = set(patch) - set(RUNTIME_SETTING_FIELDS)
        if unknown:
            raise HarnessError(
                "FIELD_NOT_EDITABLE",
                "The runtime settings patch contains a non-editable field.",
                {"fields": sorted(unknown)},
            )
        for nested in (*LIBRARY_RELEASE_FIELDS, "self_check", "advanced_model_overrides"):
            value = patch.get(nested)
            if value is not None and not isinstance(value, dict):
                raise HarnessError("VALIDATION_ERROR", f"{nested} must be an object or null.")
        self_check = patch.get("self_check") or {}
        unknown_self_check = set(self_check) - {
            "termination", "fixed_rounds", "max_rounds", "stop_early_on_pass"
        }
        advanced = patch.get("advanced_model_overrides") or {}
        unknown_models = set(advanced) - set(MODEL_STATES)
        unknown_library_fields = set()
        for nested in LIBRARY_RELEASE_FIELDS:
            boundary = patch.get(nested) or {}
            unknown_library_fields.update(set(boundary) - {"release"})
        if unknown_self_check or unknown_models or unknown_library_fields:
            raise HarnessError(
                "FIELD_NOT_EDITABLE",
                "The runtime settings patch contains a non-editable nested field.",
                {
                    "fields": sorted(
                        unknown_self_check | unknown_models | unknown_library_fields
                    )
                },
            )

    @staticmethod
    def _require_settings_actor(envelope: CommandEnvelope) -> None:
        if envelope.actor_type not in {"human", "master"}:
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only a human or Master may change instance runtime settings.",
            )

    def _validate_task_revision(self, task_id: str, expected_revision: int) -> None:
        actual = self.store.task.revision(task_id, task_id)
        if actual != expected_revision:
            raise HarnessError(
                "REVISION_CONFLICT",
                "The task revision changed before the settings command committed.",
                {"expected_revision": expected_revision, "actual_revision": actual},
            )

    @staticmethod
    def _validate_confirmation_saga(
        saga: dict[str, Any],
        *,
        task_id: str,
        instance_id: str,
        proposal_id: str,
        confirm_request_hash: str,
        idempotency_key: str,
    ) -> None:
        if (
            saga.get("task_id") != task_id
            or saga.get("instance_id") != instance_id
            or saga.get("proposal_id") != proposal_id
            or saga.get("confirm_idempotency_key") != idempotency_key
            or saga.get("confirm_request_hash") != confirm_request_hash
        ):
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The persisted settings confirmation intent is inconsistent.",
            )

    def _proposal_response(self, proposal: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "proposal_id": proposal["proposal_id"],
            "status": proposal["status"],
            "scope": {
                "task_id": proposal["task_id"],
                "instance_id": proposal["instance_id"],
            },
            "base_revision": proposal["base_revision"],
            "effective_runtime": deepcopy(proposal["effective_runtime"]),
            "effective_model_ids": deepcopy(proposal["effective_model_ids"]),
            "diff": deepcopy(proposal["diff"]),
            "apply_mode": proposal["apply_mode"],
            "workflow_boundary": deepcopy(proposal["workflow_boundary"]),
            "sync_instance_ids": deepcopy(proposal["sync_instance_ids"]),
            "created_at": proposal["created_at"],
        }

    @staticmethod
    def _saga_response(saga: dict[str, Any]) -> dict[str, Any]:
        status = {
            "APPLIED": (
                "APPLIED_BEFORE_START"
                if saga["mode"] == "before_start"
                else "APPLIED_ON_BRANCH"
            ),
            "FAILED": "FAILED",
        }.get(saga["state"], "WAITING_SAFE_POINT")
        source_bundle = saga["source"].get("bundle")
        return {
            "schema_version": "2.0",
            "proposal_id": saga["proposal_id"],
            "instance_id": saga["instance_id"],
            "status": status,
            "saga_state": saga["state"],
            "revision_id": saga["source"]["revision_id"],
            "config_hash": (
                None if source_bundle is None else source_bundle["manifest"]["config_hash"]
            ),
            "branch_id": None if saga.get("receipt") is None else saga["receipt"]["branch_id"],
            "checkpoint_id": (
                None if saga.get("receipt") is None else saga["receipt"]["checkpoint_id"]
            ),
            "sync_instance_ids": [item["instance_id"] for item in saga["sync_targets"]],
            "last_error": deepcopy(saga.get("last_error")),
        }

    def _pending_projection(
        self, task_id: str, instance_id: str
    ) -> dict[str, Any] | None:
        saga = self._pending_saga(task_id, instance_id, include_sync=True)
        if saga is None:
            return None
        projection = self._saga_response(saga)
        if saga["instance_id"] != instance_id:
            projection.update(
                {
                    "instance_id": instance_id,
                    "source_instance_id": saga["instance_id"],
                    "sync_target": True,
                }
            )
        return projection

    def _pending_saga(
        self, task_id: str, instance_id: str, *, include_sync: bool = False
    ) -> dict[str, Any] | None:
        candidates = [
            item
            for item in self._sagas_for_instance(
                task_id, instance_id, include_sync=include_sync
            )
            if item["state"] not in _TERMINAL_SAGA_STATES
        ]
        if len(candidates) > 1:
            raise HarnessError(
                "CONFIG_INTEGRITY_FAILED",
                "The instance has multiple unfinished settings sagas.",
            )
        return candidates[0] if candidates else None

    def _terminal_saga(
        self, task_id: str, instance_id: str, *, state: str | None = None
    ) -> dict[str, Any] | None:
        candidates = [
            item
            for item in self._sagas_for_instance(task_id, instance_id)
            if item["state"] in _TERMINAL_SAGA_STATES
            and (state is None or item["state"] == state)
        ]
        return max(candidates, key=lambda item: item["updated_at"]) if candidates else None

    def _sagas_for_instance(
        self, task_id: str, instance_id: str, *, include_sync: bool = False
    ) -> list[dict[str, Any]]:
        validate_identifier(task_id, "task_id")
        validate_identifier(instance_id, "instance_id")
        values = []
        for path in self.saga_root.glob("*.json"):
            saga = read_json(path)
            if saga.get("task_id") != task_id:
                continue
            owns = saga.get("instance_id") == instance_id
            syncs = include_sync and any(
                item.get("instance_id") == instance_id for item in saga.get("sync_targets", [])
            )
            if owns or syncs:
                values.append(saga)
        return values

    def _write_saga(self, saga: dict[str, Any]) -> None:
        validate_public_config_tree(saga)
        atomic_write_json(self._saga_path(saga["proposal_id"]), saga, mode=0o640)

    @staticmethod
    def _write_proposal(path: Path, proposal: dict[str, Any]) -> None:
        validate_public_config_tree(proposal)
        atomic_write_json(path, proposal, mode=0o640)

    def _update_proposal_status(self, saga: dict[str, Any], status: str) -> None:
        path = self._proposal_path(
            saga["task_id"], saga["instance_id"], saga["proposal_id"]
        )
        proposal = read_json(path)
        proposal["status"] = status
        self._write_proposal(path, proposal)

    def _update_instance_config_revision(
        self,
        task_id: str,
        instance_id: str,
        revision_id: str,
        actor: dict[str, str],
    ) -> None:
        sequence = int(revision_id.rsplit("r", 1)[1])
        self.store.update_instance_fields(
            task_id,
            instance_id,
            {"config_revision": sequence},
            actor=Actor(actor["actor_type"], actor["actor_id"]),
            command="apply_instance_runtime_config",
            idempotency_key=f"runtime-config-{revision_id}",
        )

    def _observe_latency(self, saga: dict[str, Any]) -> None:
        confirmed = datetime.fromisoformat(saga["confirmed_at"].replace("Z", "+00:00"))
        applied = datetime.fromisoformat(saga["updated_at"].replace("Z", "+00:00"))
        self.observability.observe_apply_latency((applied - confirmed).total_seconds())

    @staticmethod
    def _proposal_id(task_id: str, instance_id: str, key: str) -> str:
        digest = hashlib.sha256(f"{task_id}:{instance_id}:{key}".encode()).hexdigest()
        return f"proposal_{digest[:24]}"

    @staticmethod
    def _apply_idempotency_key(saga: dict[str, Any]) -> str:
        digest = hashlib.sha256(
            f"{saga['proposal_id']}:{saga['source']['revision_id']}".encode()
        ).hexdigest()
        return f"configapply_{digest[:24]}"

    def _proposal_path(self, task_id: str, instance_id: str, proposal_id: str) -> Path:
        validate_identifier(proposal_id, "proposal_id")
        root = self.materializer.runtime_root(task_id, instance_id) / "proposals"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root / f"{proposal_id}.json"

    def _saga_path(self, proposal_id: str) -> Path:
        validate_identifier(proposal_id, "proposal_id")
        return self.saga_root / f"{proposal_id}.json"

    def _instance_gate_path(self, task_id: str, instance_id: str) -> Path:
        return (
            self.store.layout.control_root
            / "locks"
            / f"runtime-config-instance-{task_id}-{instance_id}.lock"
        )
