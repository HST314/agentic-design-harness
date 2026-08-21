"""Atomic automatic-retry budgets and human one-shot overrides."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..storage.atomic import digest_json
from ..storage.layout import validate_identifier
from ..storage.locks import FileLock
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .approvals import ApprovalInboxService


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_auto_retries_per_retry_group: int = Field(default=0, ge=0, le=100)
    max_auto_retry_tokens_task: int = Field(default=0, ge=0)
    retry_token_reservation_by_agent: dict[str, int] = Field(default_factory=dict)
    max_auto_retry_cost_micros: int | None = Field(default=None, ge=0)
    price_catalog_revision: str | None = Field(
        default=None, pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$"
    )

    @field_validator("retry_token_reservation_by_agent")
    @classmethod
    def validate_reservations(cls, value: dict[str, int]) -> dict[str, int]:
        if set(value) - {"image", "ppt", "master"}:
            raise ValueError("retry reservations contain an unknown Agent type")
        if any(isinstance(item, bool) or item <= 0 for item in value.values()):
            raise ValueError("retry reservations must be positive integers")
        return value


class RetryBudgetService:
    """Keep check, reservation and attempt lineage in one task lock."""

    def __init__(self, store: FileStateStore, approvals: ApprovalInboxService) -> None:
        self.store = store
        self.approvals = approvals

    def get(self, task_id: str) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        self._task(task_id)
        current = self.store.retry_budget.get(task_id, task_id)
        return self._initial_snapshot(task_id) if current is None else deepcopy(current)

    def configure(
        self,
        task_id: str,
        policy: dict[str, Any],
        *,
        expected_revision: int,
        idempotency_key: str,
        actor: Actor,
    ) -> dict[str, Any]:
        validate_identifier(task_id, "task_id")
        validate_identifier(idempotency_key, "idempotency_key")
        if actor.actor_type != "human":
            raise HarnessError(
                "VALIDATION_ERROR", "Only a human may revise an automatic retry budget."
            )
        validated = self._policy(policy)
        request = {
            "task_id": task_id,
            "policy": validated.model_dump(mode="json"),
            "expected_revision": expected_revision,
        }
        request_sha256 = digest_json({"command": "configure_retry_budget", "payload": request})
        with FileLock(self._lock_path(task_id), self.store.lock_timeout_seconds):
            committed = self.store.lookup_committed_command_result(
                task_id,
                idempotency_key,
                "configure_retry_budget",
                request_sha256,
            )
            if committed is not None:
                return committed
            current = self.get(task_id)
            if current["revision"] != expected_revision:
                self._revision_error(expected_revision, current["revision"])
            updated = deepcopy(current)
            updated.update(
                {
                    "revision": current["revision"] + 1,
                    "retry_policy": validated.model_dump(mode="json"),
                    "updated_at": utc_now(),
                }
            )
            result = deepcopy(updated)
            self.store.retry_budget.put(
                task_id,
                task_id,
                updated,
                expected_revision=self.store.retry_budget.revision(task_id, task_id),
                actor=actor,
                command="configure_retry_budget",
                idempotency_key=idempotency_key,
                command_result=result,
                request_sha256=request_sha256,
            )
            return result

    def request_retry(
        self,
        task_id: str,
        instance_id: str,
        *,
        attempt_id: str,
        retry_group_id: str,
        retry_of_attempt_id: str,
        idempotency_key: str,
        actor: Actor,
        reservation_tokens: int | None = None,
        estimated_cost_micros: int | None = None,
        price_catalog_revision: str | None = None,
    ) -> dict[str, Any]:
        for value, name in (
            (task_id, "task_id"),
            (instance_id, "instance_id"),
            (attempt_id, "attempt_id"),
            (retry_group_id, "retry_group_id"),
            (retry_of_attempt_id, "retry_of_attempt_id"),
            (idempotency_key, "idempotency_key"),
        ):
            validate_identifier(value, name)
        if actor.actor_type not in {"master", "system"}:
            raise HarnessError(
                "VALIDATION_ERROR", "Automatic retries may only be requested by Master or system."
            )
        if attempt_id == retry_of_attempt_id:
            raise HarnessError("VALIDATION_ERROR", "A retry attempt cannot point to itself.")
        instance = self._instance(task_id, instance_id)
        if reservation_tokens is not None and (
            isinstance(reservation_tokens, bool) or reservation_tokens <= 0
        ):
            raise HarnessError("VALIDATION_ERROR", "Retry Token reservation is invalid.")
        if estimated_cost_micros is not None and (
            isinstance(estimated_cost_micros, bool) or estimated_cost_micros < 0
        ):
            raise HarnessError("VALIDATION_ERROR", "Retry cost reservation is invalid.")
        request = {
            "task_id": task_id,
            "instance_id": instance_id,
            "attempt_id": attempt_id,
            "retry_group_id": retry_group_id,
            "retry_of_attempt_id": retry_of_attempt_id,
            "reservation_tokens": reservation_tokens,
            "estimated_cost_micros": estimated_cost_micros,
            "price_catalog_revision": price_catalog_revision,
        }
        request_sha256 = digest_json({"command": "reserve_retry", "payload": request})
        with FileLock(self._lock_path(task_id), self.store.lock_timeout_seconds):
            committed = self.store.lookup_committed_command_result(
                task_id,
                idempotency_key,
                "reserve_retry",
                request_sha256,
            )
            if committed is not None:
                if not committed["allowed"]:
                    self._ensure_denial_approval(committed)
                    self._raise_denied(committed)
                return committed
            snapshot = self.get(task_id)
            if any(item["attempt_id"] == attempt_id for item in snapshot["attempts"]):
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT",
                    "The retry attempt id already belongs to another request.",
                )
            lineage_groups = {
                item["retry_group_id"]
                for item in snapshot["attempts"]
                if item["attempt_id"] == retry_of_attempt_id
                or item["retry_of_attempt_id"] == retry_of_attempt_id
            }
            if lineage_groups and lineage_groups != {retry_group_id}:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A replacement retry must retain its original retry group lineage.",
                    {"retry_group_id": sorted(lineage_groups)[0]},
                )
            policy = RetryPolicy.model_validate(snapshot["retry_policy"])
            configured_reservation = policy.retry_token_reservation_by_agent.get(
                instance["agent_type"]
            )
            if (
                configured_reservation is not None
                and reservation_tokens is not None
                and reservation_tokens != configured_reservation
            ):
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A retry cannot override the configured Agent Token reservation.",
                )
            requested_tokens = configured_reservation or reservation_tokens
            gates = self._gates(
                snapshot,
                policy,
                retry_group_id=retry_group_id,
                requested_tokens=requested_tokens,
                estimated_cost_micros=estimated_cost_micros,
                price_catalog_revision=price_catalog_revision,
            )
            approval_id, _ = self.approvals.budget_approval_identity(
                task_id, instance_id, attempt_id
            )
            now = utc_now()
            attempt = {
                "attempt_id": attempt_id,
                "retry_group_id": retry_group_id,
                "retry_of_attempt_id": retry_of_attempt_id,
                "instance_id": instance_id,
                "agent_type": instance["agent_type"],
                "automatic": True,
                "status": "PENDING_APPROVAL" if gates else "RESERVED",
                "reserved_tokens": 0 if requested_tokens is None else requested_tokens,
                "reserved_cost_micros": estimated_cost_micros,
                "price_catalog_revision": price_catalog_revision,
                "approval_id": approval_id if gates else None,
                "denied_gates": gates,
                "override_consumed_at": None,
                "actual_tokens": None,
                "actual_cost_micros": None,
                "created_at": now,
                "settled_at": None,
            }
            updated = deepcopy(snapshot)
            updated["revision"] += 1
            updated["attempts"].append(attempt)
            if not gates:
                ledger = updated["retry_budget_ledger"]
                ledger["retry_tokens_reserved"] += int(requested_tokens)
                if estimated_cost_micros is not None:
                    ledger["retry_cost_micros_reserved"] += estimated_cost_micros
            updated["updated_at"] = now
            result = {
                "schema_version": "1.0",
                "allowed": not gates,
                "task_id": task_id,
                "instance_id": instance_id,
                "attempt": deepcopy(attempt),
                "denied_gates": deepcopy(gates),
                "approval_id": approval_id if gates else None,
                "budget_revision": updated["revision"],
                "ledger": deepcopy(updated["retry_budget_ledger"]),
            }
            self.store.retry_budget.put(
                task_id,
                task_id,
                updated,
                expected_revision=self.store.retry_budget.revision(task_id, task_id),
                actor=actor,
                command="reserve_retry",
                idempotency_key=idempotency_key,
                command_result=result,
                request_sha256=request_sha256,
            )
        if gates:
            self._ensure_denial_approval(result)
            self._raise_denied(result)
        return result

    def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: str,
        action: str | None,
        payload: dict[str, Any],
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        details = self.approvals.get_approval(approval_id)
        approval = details["approval"]
        approval_payload = details["payload"]
        request_sha256 = self._resolution_digest(approval_id, decision, action, payload)
        budget_idempotency_key = f"budget-{envelope.idempotency_key}"
        if approval["kind"] != "BUDGET_OVERRIDE":
            raise HarnessError("VALIDATION_ERROR", "The approval is not a budget override.")
        if decision == "APPROVED":
            if action != "approve_once" or set(payload) - {
                "token_limit",
                "cost_limit_micros",
            }:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "A budget override only accepts explicit one-shot limits.",
                )
            for field in ("token_limit", "cost_limit_micros"):
                value = payload.get(field)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or value < (1 if field == "token_limit" else 0)
                ):
                    raise HarnessError("VALIDATION_ERROR", "A one-shot budget limit is invalid.")
        elif decision == "REJECTED":
            if action is not None or payload:
                raise HarnessError(
                    "VALIDATION_ERROR", "A rejected budget override has no action payload."
                )
        else:
            raise HarnessError("VALIDATION_ERROR", "The approval decision is invalid.")
        task_id = approval["task_id"]
        attempt_id = approval_payload.get("attempt_id")
        validate_identifier(attempt_id, "attempt_id")
        with FileLock(self._lock_path(task_id), self.store.lock_timeout_seconds):
            committed = self.store.lookup_committed_command_result(
                task_id,
                budget_idempotency_key,
                "resolve_budget_approval",
                request_sha256,
            )
            if committed is not None:
                self._handle_resolution_notification(
                    approval_id, envelope.idempotency_key, envelope
                )
                return committed
            snapshot = self.get(task_id)
            attempt = self._attempt(snapshot, attempt_id)
            if attempt.get("approval_id") != approval_id:
                raise HarnessError(
                    "IDEMPOTENCY_CONFLICT", "The approval is not bound to this retry attempt."
                )
            pending = attempt.get("pending_resolution")
            if pending is not None:
                if (
                    pending.get("request_sha256") != request_sha256
                    or pending.get("idempotency_key") != envelope.idempotency_key
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "Another human budget decision is already being committed.",
                    )
                return self._finish_pending_resolution(task_id, snapshot, attempt)
            details = self.approvals.get_approval(approval_id)
            approval = details["approval"]
            if approval["status"] != "PENDING" or attempt["status"] != "PENDING_APPROVAL":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION", "The budget approval was already resolved."
                )
            if envelope.actor_type != approval["owner"]:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "The approval must be resolved by its frozen owner.",
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
            denied = {item["gate"] for item in attempt["denied_gates"]}
            if decision == "APPROVED" and (
                "TOKEN_BOUND_UNKNOWN" in denied and "token_limit" not in payload
            ):
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "This override requires an explicit one-shot Token limit.",
                )
            if decision == "APPROVED" and (
                "COST_UNKNOWN" in denied and "cost_limit_micros" not in payload
            ):
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "This override requires an explicit one-shot cost limit.",
                )
            attempt["pending_resolution"] = {
                "approval_id": approval_id,
                "decision": decision,
                "action": action,
                "payload": deepcopy(payload),
                "idempotency_key": envelope.idempotency_key,
                "request_sha256": request_sha256,
                "envelope": envelope.model_dump(mode="json"),
                "prepared_at": utc_now(),
            }
            snapshot["revision"] += 1
            snapshot["updated_at"] = utc_now()
            self.store.retry_budget.put(
                task_id,
                task_id,
                snapshot,
                expected_revision=self.store.retry_budget.revision(task_id, task_id),
                actor=Actor(envelope.actor_type, envelope.actor_id),
                command="prepare_budget_resolution",
                idempotency_key=f"budget-prepare-{envelope.idempotency_key}",
                command_result={
                    "approval_id": approval_id,
                    "attempt_id": attempt_id,
                    "budget_revision": snapshot["revision"],
                },
                request_sha256=request_sha256,
            )
            return self._finish_pending_resolution(task_id, snapshot, attempt)

    def consume_override(
        self,
        task_id: str,
        attempt_id: str,
        *,
        idempotency_key: str,
        actor: Actor,
    ) -> dict[str, Any]:
        validate_identifier(attempt_id, "attempt_id")
        validate_identifier(idempotency_key, "idempotency_key")
        if actor.actor_type != "system":
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only the retry executor may consume a one-shot budget override.",
            )
        request_sha256 = digest_json(
            {
                "command": "consume_budget_override",
                "payload": {"task_id": task_id, "attempt_id": attempt_id},
            }
        )
        with FileLock(self._lock_path(task_id), self.store.lock_timeout_seconds):
            committed = self.store.lookup_committed_command_result(
                task_id,
                idempotency_key,
                "consume_budget_override",
                request_sha256,
            )
            if committed is not None:
                return committed
            snapshot = self.get(task_id)
            attempt = self._attempt(snapshot, attempt_id)
            if attempt["status"] == "RESERVED" and attempt["override_consumed_at"]:
                return deepcopy(attempt)
            if attempt["status"] != "AUTHORIZED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "The one-shot retry override is not available.",
                )
            attempt["status"] = "RESERVED"
            attempt["override_consumed_at"] = utc_now()
            ledger = snapshot["retry_budget_ledger"]
            ledger["retry_tokens_reserved"] += int(attempt["reserved_tokens"])
            if attempt["reserved_cost_micros"] is not None:
                ledger["retry_cost_micros_reserved"] += int(attempt["reserved_cost_micros"])
            snapshot["revision"] += 1
            snapshot["updated_at"] = utc_now()
            result = deepcopy(attempt)
            self.store.retry_budget.put(
                task_id,
                task_id,
                snapshot,
                expected_revision=self.store.retry_budget.revision(task_id, task_id),
                actor=actor,
                command="consume_budget_override",
                idempotency_key=idempotency_key,
                command_result=result,
                request_sha256=request_sha256,
            )
            return result

    def settle(
        self,
        task_id: str,
        attempt_id: str,
        *,
        actual_tokens: int,
        actual_cost_micros: int | None,
        idempotency_key: str,
        actor: Actor,
    ) -> dict[str, Any]:
        validate_identifier(attempt_id, "attempt_id")
        validate_identifier(idempotency_key, "idempotency_key")
        if isinstance(actual_tokens, bool) or actual_tokens < 0:
            raise HarnessError("VALIDATION_ERROR", "Settled retry Token usage is invalid.")
        if actual_cost_micros is not None and (
            isinstance(actual_cost_micros, bool) or actual_cost_micros < 0
        ):
            raise HarnessError("VALIDATION_ERROR", "Settled retry cost is invalid.")
        request_sha256 = digest_json(
            {
                "command": "settle_retry",
                "payload": {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "actual_tokens": actual_tokens,
                    "actual_cost_micros": actual_cost_micros,
                },
            }
        )
        with FileLock(self._lock_path(task_id), self.store.lock_timeout_seconds):
            snapshot = self.get(task_id)
            attempt = self._attempt(snapshot, attempt_id)
            if actor.actor_type not in {"adapter", "system"} or (
                actor.actor_type == "adapter"
                and actor.actor_id != f"{attempt['agent_type']}_adapter"
            ):
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "Only the owning Adapter or retry executor may settle retry usage.",
                )
            committed = self.store.lookup_committed_command_result(
                task_id,
                idempotency_key,
                "settle_retry",
                request_sha256,
            )
            if committed is not None:
                return committed
            if attempt["status"] in {"SETTLED", "EXCEEDED"}:
                if (
                    attempt["actual_tokens"] != actual_tokens
                    or attempt["actual_cost_micros"] != actual_cost_micros
                ):
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The retry attempt was settled with different usage.",
                    )
                return {
                    "attempt": deepcopy(attempt),
                    "ledger": deepcopy(snapshot["retry_budget_ledger"]),
                }
            if attempt["status"] != "RESERVED":
                raise HarnessError(
                    "INVALID_STATE_TRANSITION", "Only a reserved retry may be settled."
                )
            ledger = snapshot["retry_budget_ledger"]
            reserved_tokens = int(attempt["reserved_tokens"])
            ledger["retry_tokens_reserved"] = max(
                0, ledger["retry_tokens_reserved"] - reserved_tokens
            )
            ledger["retry_tokens_settled"] += actual_tokens
            reserved_cost = attempt["reserved_cost_micros"]
            if reserved_cost is not None:
                ledger["retry_cost_micros_reserved"] = max(
                    0, ledger["retry_cost_micros_reserved"] - reserved_cost
                )
            if actual_cost_micros is None:
                ledger["unknown_cost_settlements"] += 1
                ledger["retry_cost_micros_settled"] = None
            elif ledger["unknown_cost_settlements"] == 0:
                prior = ledger["retry_cost_micros_settled"] or 0
                ledger["retry_cost_micros_settled"] = prior + actual_cost_micros
            exceeded = actual_tokens > reserved_tokens or (
                reserved_cost is not None
                and actual_cost_micros is not None
                and actual_cost_micros > reserved_cost
            )
            if exceeded:
                ledger["frozen"] = True
                ledger["frozen_reason"] = "ACTUAL_USAGE_EXCEEDED_RESERVATION"
            attempt.update(
                {
                    "status": "EXCEEDED" if exceeded else "SETTLED",
                    "actual_tokens": actual_tokens,
                    "actual_cost_micros": actual_cost_micros,
                    "settled_at": utc_now(),
                }
            )
            snapshot["revision"] += 1
            snapshot["updated_at"] = utc_now()
            result = {
                "attempt": deepcopy(attempt),
                "ledger": deepcopy(ledger),
                "budget_revision": snapshot["revision"],
            }
            self.store.retry_budget.put(
                task_id,
                task_id,
                snapshot,
                expected_revision=self.store.retry_budget.revision(task_id, task_id),
                actor=actor,
                command="settle_retry",
                idempotency_key=idempotency_key,
                command_result=result,
                request_sha256=request_sha256,
            )
            return result

    def recover(self) -> list[dict[str, Any]]:
        recovered: list[dict[str, Any]] = []
        tasks_root = self.store.layout.control_root / "tasks"
        for task_dir in sorted(tasks_root.iterdir() if tasks_root.exists() else []):
            if not task_dir.is_dir():
                continue
            with FileLock(self._lock_path(task_dir.name), self.store.lock_timeout_seconds):
                snapshot = self.store.retry_budget.get(task_dir.name, task_dir.name)
                if snapshot is None:
                    continue
                for candidate in list(snapshot["attempts"]):
                    attempt = self._attempt(snapshot, candidate["attempt_id"])
                    if attempt.get("pending_resolution") is not None:
                        self._finish_pending_resolution(task_dir.name, snapshot, attempt)
                        snapshot = self.get(task_dir.name)
                        recovered.append(
                            {
                                "task_id": task_dir.name,
                                "attempt_id": attempt["attempt_id"],
                                "approval_id": attempt["approval_id"],
                                "resolution_completed": True,
                            }
                        )
                        continue
                    if attempt["status"] == "PENDING_APPROVAL":
                        result = {
                            "task_id": task_dir.name,
                            "instance_id": attempt["instance_id"],
                            "attempt": attempt,
                            "denied_gates": attempt["denied_gates"],
                            "approval_id": attempt["approval_id"],
                            "budget_revision": snapshot["revision"],
                        }
                        self._ensure_denial_approval(result)
                        recovered.append(
                            {
                                "task_id": task_dir.name,
                                "attempt_id": attempt["attempt_id"],
                                "approval_id": attempt["approval_id"],
                            }
                        )
                    elif attempt.get("resolution") is not None:
                        resolution = attempt["resolution"]
                        self._handle_resolution_notification(
                            attempt["approval_id"],
                            resolution["idempotency_key"],
                            CommandEnvelope.model_validate(resolution["envelope"]),
                        )
        return recovered

    def _finish_pending_resolution(
        self,
        task_id: str,
        snapshot: dict[str, Any],
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        pending = attempt.get("pending_resolution")
        if not isinstance(pending, dict):
            raise RuntimeError("retry budget resolution is not prepared")
        envelope = CommandEnvelope.model_validate(pending["envelope"])
        approval_id = pending["approval_id"]
        decision = pending["decision"]
        payload = pending["payload"]
        committed = self.approvals.commit_resolution(approval_id, decision, envelope)
        if decision == "APPROVED":
            if "token_limit" in payload:
                attempt["reserved_tokens"] = payload["token_limit"]
            if "cost_limit_micros" in payload:
                attempt["reserved_cost_micros"] = payload["cost_limit_micros"]
        attempt["status"] = "AUTHORIZED" if decision == "APPROVED" else "CANCELLED"
        attempt["resolved_at"] = utc_now()
        attempt["resolved_by"] = {
            "actor_type": envelope.actor_type,
            "actor_id": envelope.actor_id,
        }
        attempt["resolution"] = {
            "idempotency_key": envelope.idempotency_key,
            "envelope": envelope.model_dump(mode="json"),
        }
        attempt["pending_resolution"] = None
        snapshot["revision"] += 1
        snapshot["updated_at"] = utc_now()
        result = {
            **committed,
            "attempt": deepcopy(attempt),
            "budget_revision": snapshot["revision"],
        }
        self.store.retry_budget.put(
            task_id,
            task_id,
            snapshot,
            expected_revision=self.store.retry_budget.revision(task_id, task_id),
            actor=Actor(envelope.actor_type, envelope.actor_id),
            command="resolve_budget_approval",
            idempotency_key=f"budget-{envelope.idempotency_key}",
            command_result=result,
            request_sha256=pending["request_sha256"],
        )
        self._handle_resolution_notification(approval_id, envelope.idempotency_key, envelope)
        return result

    def _handle_resolution_notification(
        self,
        approval_id: str,
        idempotency_key: str,
        envelope: CommandEnvelope,
    ) -> None:
        self.approvals.handle_approval_notification(
            approval_id,
            Actor(envelope.actor_type, envelope.actor_id),
            f"handle-budget-{idempotency_key}",
        )

    @staticmethod
    def _resolution_digest(
        approval_id: str,
        decision: str,
        action: str | None,
        payload: dict[str, Any],
    ) -> str:
        return digest_json(
            {
                "command": "resolve_budget_approval",
                "payload": {
                    "approval_id": approval_id,
                    "decision": decision,
                    "action": action,
                    "payload": payload,
                },
            }
        )

    def _gates(
        self,
        snapshot: dict[str, Any],
        policy: RetryPolicy,
        *,
        retry_group_id: str,
        requested_tokens: int | None,
        estimated_cost_micros: int | None,
        price_catalog_revision: str | None,
    ) -> list[dict[str, Any]]:
        ledger = snapshot["retry_budget_ledger"]
        gates: list[dict[str, Any]] = []
        group_count = sum(
            1
            for item in snapshot["attempts"]
            if item["retry_group_id"] == retry_group_id
            and item["status"] in {"RESERVED", "SETTLED", "EXCEEDED"}
        )
        if group_count + 1 > policy.max_auto_retries_per_retry_group:
            gates.append(
                {
                    "gate": "RETRY_COUNT",
                    "used": group_count,
                    "requested": 1,
                    "limit": policy.max_auto_retries_per_retry_group,
                }
            )
        if requested_tokens is None:
            gates.append(
                {
                    "gate": "TOKEN_BOUND_UNKNOWN",
                    "used": ledger["retry_tokens_settled"],
                    "reserved": ledger["retry_tokens_reserved"],
                    "requested": None,
                    "limit": policy.max_auto_retry_tokens_task,
                }
            )
        elif (
            ledger["retry_tokens_settled"] + ledger["retry_tokens_reserved"] + requested_tokens
            > policy.max_auto_retry_tokens_task
        ):
            gates.append(
                {
                    "gate": "TOKEN_LIMIT",
                    "used": ledger["retry_tokens_settled"],
                    "reserved": ledger["retry_tokens_reserved"],
                    "requested": requested_tokens,
                    "limit": policy.max_auto_retry_tokens_task,
                }
            )
        if policy.max_auto_retry_cost_micros is not None:
            if (
                estimated_cost_micros is None
                or price_catalog_revision is None
                or price_catalog_revision != policy.price_catalog_revision
            ):
                gates.append(
                    {
                        "gate": "COST_UNKNOWN",
                        "used": ledger["retry_cost_micros_settled"],
                        "reserved": ledger["retry_cost_micros_reserved"],
                        "requested": estimated_cost_micros,
                        "limit": policy.max_auto_retry_cost_micros,
                        "price_catalog_revision": policy.price_catalog_revision,
                    }
                )
            else:
                settled_cost = ledger["retry_cost_micros_settled"]
                if settled_cost is None:
                    gates.append(
                        {
                            "gate": "COST_UNKNOWN",
                            "used": None,
                            "reserved": ledger["retry_cost_micros_reserved"],
                            "requested": estimated_cost_micros,
                            "limit": policy.max_auto_retry_cost_micros,
                            "price_catalog_revision": policy.price_catalog_revision,
                        }
                    )
                elif (
                    settled_cost + ledger["retry_cost_micros_reserved"] + estimated_cost_micros
                    > policy.max_auto_retry_cost_micros
                ):
                    gates.append(
                        {
                            "gate": "COST_LIMIT",
                            "used": settled_cost,
                            "reserved": ledger["retry_cost_micros_reserved"],
                            "requested": estimated_cost_micros,
                            "limit": policy.max_auto_retry_cost_micros,
                            "price_catalog_revision": policy.price_catalog_revision,
                        }
                    )
        if ledger["frozen"]:
            gates.append(
                {
                    "gate": "LEDGER_FROZEN",
                    "reason": ledger["frozen_reason"],
                }
            )
        return gates

    def _ensure_denial_approval(self, result: dict[str, Any]) -> None:
        attempt = result["attempt"]
        self.approvals.ensure_budget_approval(
            result["task_id"],
            result["instance_id"],
            attempt_id=attempt["attempt_id"],
            context={
                "denied_gates": deepcopy(result["denied_gates"]),
                "retry_group_id": attempt["retry_group_id"],
                "retry_of_attempt_id": attempt["retry_of_attempt_id"],
                "reserved_tokens": attempt["reserved_tokens"],
                "reserved_cost_micros": attempt["reserved_cost_micros"],
                "budget_revision": result["budget_revision"],
            },
        )

    @staticmethod
    def _raise_denied(result: dict[str, Any]) -> None:
        raise HarnessError(
            "BUDGET_GATE_DENIED",
            "The automatic retry requires a one-shot human budget approval.",
            {
                "attempt_id": result["attempt"]["attempt_id"],
                "approval_id": result["approval_id"],
                "budget_revision": result["budget_revision"],
                "gates": deepcopy(result["denied_gates"]),
            },
        )

    def _initial_snapshot(self, task_id: str) -> dict[str, Any]:
        now = utc_now()
        return {
            "schema_version": "1.0",
            "task_id": task_id,
            "revision": 0,
            "retry_policy": RetryPolicy().model_dump(mode="json"),
            "retry_budget_ledger": {
                "retry_tokens_reserved": 0,
                "retry_tokens_settled": 0,
                "retry_cost_micros_reserved": 0,
                "retry_cost_micros_settled": 0,
                "unknown_cost_settlements": 0,
                "frozen": False,
                "frozen_reason": None,
            },
            "attempts": [],
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _attempt(snapshot: dict[str, Any], attempt_id: str) -> dict[str, Any]:
        attempt = next(
            (item for item in snapshot["attempts"] if item["attempt_id"] == attempt_id),
            None,
        )
        if attempt is None:
            raise HarnessError("VALIDATION_ERROR", "The retry attempt does not exist.")
        return attempt

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.store.task.get(task_id, task_id)
        if task is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        return task

    def _instance(self, task_id: str, instance_id: str) -> dict[str, Any]:
        self._task(task_id)
        instance = self.store.instance.get(task_id, instance_id)
        if instance is None or instance.get("task_id") != task_id:
            raise HarnessError("INSTANCE_NOT_FOUND", "The requested instance does not exist.")
        return instance

    def _lock_path(self, task_id: str):
        return self.store.layout.control_root / "locks" / f"retry-budget-{task_id}.lock"

    @staticmethod
    def _policy(value: dict[str, Any]) -> RetryPolicy:
        try:
            return RetryPolicy.model_validate(value)
        except ValidationError as exc:
            raise HarnessError(
                "VALIDATION_ERROR",
                "The automatic retry policy is invalid.",
                {"error_count": exc.error_count()},
            ) from None

    @staticmethod
    def _revision_error(expected: int, actual: int) -> None:
        raise HarnessError(
            "REVISION_CONFLICT",
            "The retry budget revision changed before the command committed.",
            {"expected_revision": expected, "actual_revision": actual},
        )
