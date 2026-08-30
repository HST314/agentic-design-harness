"""Persistent Master conversation, planning and recoverable confirmation workflows."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

from ..adapters import AdapterRegistry
from ..adapters.types import AgentInstanceSnapshot, StageSnapshot, TaskCard
from ..core.errors import HarnessError
from ..domain.commands import CommandEnvelope
from ..domain.master import materialize_plan_proposal
from ..domain.service import TaskCommandService
from ..storage.atomic import atomic_write_json, read_json
from ..storage.repository import Actor, utc_now
from ..storage.store import FileStateStore
from .application import HarnessApplicationService
from .assets import AssetService
from .master_orchestrator import (
    MasterOrchestratorFailure,
    MasterPlanner,
    MasterRunObservation,
)
from .plan_proposals import PlanProposalValidationService
from .start_operations import StartOperationRunner

_EDITABLE_INSTANCE_STATUSES = frozenset({"READY", "UNAVAILABLE"})


class MasterThreadService:
    """Keep one durable Master thread per task around in-process orchestration."""

    def __init__(
        self,
        store: FileStateStore,
        commands: TaskCommandService,
        application: HarnessApplicationService,
        assets: AssetService,
        adapters: AdapterRegistry,
        orchestrator: MasterPlanner,
        plan_proposals: PlanProposalValidationService,
    ) -> None:
        self.store = store
        self.commands = commands
        self.application = application
        self.assets = assets
        self.adapters = adapters
        self.orchestrator = orchestrator
        self.plan_proposals = plan_proposals
        self.intent_root = store.layout.control_root / "master-intents"
        self.intent_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.run_monitor = StartOperationRunner(
            self._run_pending_master,
            interval_seconds=0.5,
            thread_name="harness-master-runs",
        )

    @property
    def run_monitor_alive(self) -> bool:
        return self.run_monitor.alive

    def start_monitoring(self) -> None:
        self.run_monitor.start()
        self.run_monitor.notify()

    def close_monitoring(self) -> None:
        self.run_monitor.close()

    def get_session(self, task_id: str, *, reconcile: bool = False) -> dict[str, Any]:
        """Return a persisted snapshot; browser polling never advances domain state."""

        self._task(task_id)
        if self.store.master_thread.get(task_id, task_id) is None:
            with self.commands.task_guard(task_id):
                task = self._task(task_id)
                thread = self._ensure_thread(task_id, task)
                self._ensure_intake_message(task_id, task, thread)
        if reconcile:
            self.run_monitor.notify()
        return self._session(task_id)

    def ensure_intake_started(self, task_id: str) -> dict[str, Any]:
        """Bridge an already committed F1 intake to Master without weakening submit."""

        with self.commands.task_guard(task_id):
            task = self._task(task_id)
            thread = self._ensure_thread(task_id, task)
            thread = self._ensure_intake_message(task_id, task, thread)
            active = thread["active_run"]
            if active is not None and active["status"] in {"SUBMITTING", "RUNNING"}:
                self._advance_active_run(task_id, thread)
        self.run_monitor.notify()
        return self._session(task_id)

    def recover(self) -> list[dict[str, Any]]:
        """Resume confirmation intents and one polling step for active durable runs."""

        recovered: list[dict[str, Any]] = []
        for path in sorted(self.intent_root.glob("*.json")):
            intent = read_json(path)
            task_id = intent.get("task_id")
            if intent.get("kind") != "CONFIRM_MASTER_PLAN" or not isinstance(task_id, str):
                raise HarnessError("VALIDATION_ERROR", "A Master confirmation intent is invalid.")
            if intent.get("state") in {"COMPLETED", "ABORTED"}:
                continue
            with self.commands.task_guard(task_id):
                result = self._resume_confirm(path)
            recovered.append({
                "kind": "confirmation",
                "task_id": task_id,
                "proposal_revision": intent["proposal_revision"],
                "status": result["proposal"]["status"],
            })

        tasks_root = self.store.layout.control_root / "tasks"
        for thread_path in sorted(tasks_root.glob("*/master/thread.json")):
            task_id = thread_path.parents[1].name
            with self.commands.task_guard(task_id):
                thread = self.store.master_thread.get(task_id, task_id)
                if thread is None:
                    continue
                active = thread["active_run"]
                if active is None or active["status"] not in {"SUBMITTING", "RUNNING"}:
                    continue
                advanced = self._advance_active_run(task_id, thread)
                recovered.append({
                    "kind": "run",
                    "task_id": task_id,
                    "run_id": advanced["active_run"]["run_id"],
                    "status": advanced["active_run"]["status"],
                })
        return recovered

    def append_message(
        self,
        task_id: str,
        *,
        content: str,
        asset_refs: list[dict[str, str]],
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        if envelope.actor_type != "human":
            raise HarnessError("VALIDATION_ERROR", "Only a human may add a Master message.")
        self._require_task_not_archived(task_id)
        normalized = content.strip()
        if not normalized or len(normalized) > 20_000:
            raise HarnessError("VALIDATION_ERROR", "The Master message is invalid.")
        refs = self._validate_asset_refs(task_id, asset_refs)
        message_id = self._identifier("message", task_id, envelope.idempotency_key)
        with self.commands.task_guard(task_id):
            task = self._task(task_id)
            thread = self._ensure_thread(task_id, task)
            existing = self.store.master_message.get(task_id, message_id)
            if existing is not None:
                if existing["content"] != normalized or existing["asset_refs"] != refs:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The message idempotency key was reused with different content.",
                    )
                return self._session(task_id)
            actual = self.store.master_thread.revision(task_id, task_id)
            if envelope.expected_revision != actual:
                self._revision_conflict(
                    "master_thread", task_id, envelope.expected_revision, actual
                )
            active = thread["active_run"]
            if active is not None and active["status"] in {"SUBMITTING", "RUNNING"}:
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "Wait for the active Master run before sending another message.",
                )
            message = self._message(
                task_id,
                message_id,
                thread["latest_sequence"] + 1,
                role="user",
                kind="text",
                content=normalized,
                asset_refs=refs,
            )
            self._put_message(
                message,
                Actor(envelope.actor_type, envelope.actor_id),
                "append_master_message",
            )
            now = utc_now()
            thread = self._put_thread(
                task_id,
                {
                    **thread,
                    "latest_sequence": message["sequence"],
                    "active_run": {
                        "run_id": None,
                        "message_id": message_id,
                        "status": "SUBMITTING",
                        "started_at": now,
                        "updated_at": now,
                    },
                    "last_error": None,
                },
                Actor(envelope.actor_type, envelope.actor_id),
                "queue_master_run",
            )
            self._advance_active_run(task_id, thread)
            self.run_monitor.notify()
            return self._session(task_id)

    def _run_pending_master(self) -> None:
        tasks_root = self.store.layout.control_root / "tasks"
        for thread_path in sorted(tasks_root.glob("*/master/thread.json")):
            task_id = thread_path.parents[1].name
            run_id: str | None = None
            with self.commands.task_guard(task_id):
                thread = self.store.master_thread.get(task_id, task_id)
                if thread is None:
                    continue
                active = thread["active_run"]
                if active is None or active["status"] not in {"SUBMITTING", "RUNNING"}:
                    continue
                thread = self._advance_active_run(task_id, thread)
                active = thread["active_run"]
                if (
                    active is not None
                    and active["status"] == "RUNNING"
                    and isinstance(active.get("run_id"), str)
                ):
                    run_id = active["run_id"]
            if run_id is None:
                continue

            # Model and asset-tool work may take tens of seconds. The durable run
            # is already checkpointed, so execute it without monopolizing the
            # task command lock, then briefly reacquire the lock to publish the
            # terminal observation into the Master thread.
            self.orchestrator.execute_run(task_id, run_id)
            with self.commands.task_guard(task_id):
                thread = self.store.master_thread.get(task_id, task_id)
                if thread is None:
                    continue
                active = thread["active_run"]
                if (
                    active is None
                    or active["status"] != "RUNNING"
                    or active.get("run_id") != run_id
                ):
                    continue
                self._advance_active_run(task_id, thread)

    def confirm_plan(
        self,
        task_id: str,
        proposal_revision: int,
        *,
        task_expected_revision: int,
        expected_card_revisions: dict[str, int],
        envelope: CommandEnvelope,
        instance_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._confirm(
            task_id,
            proposal_revision,
            task_expected_revision=task_expected_revision,
            expected_card_revisions=expected_card_revisions,
            envelope=envelope,
            instance_ids=instance_ids,
        )

    def revise_task_card(
        self,
        task_id: str,
        proposal_revision: int,
        card_id: str,
        *,
        expected_proposal_revision: int,
        expected_card_revision: int,
        editable: dict[str, Any],
        envelope: CommandEnvelope,
    ) -> dict[str, Any]:
        """Create a pending proposal revision for a card that has not started."""

        if envelope.actor_type != "human":
            raise HarnessError("VALIDATION_ERROR", "Only a human may revise a task card.")
        self._require_task_not_archived(task_id)
        if proposal_revision != expected_proposal_revision:
            self._revision_conflict(
                "plan_proposal",
                f"{task_id}:r{proposal_revision}",
                expected_proposal_revision,
                proposal_revision,
            )
        if envelope.expected_revision != expected_proposal_revision:
            self._revision_conflict(
                "plan_proposal",
                f"{task_id}:r{proposal_revision}",
                envelope.expected_revision,
                expected_proposal_revision,
            )
        next_proposal_id = self._identifier(
            "proposal_edit",
            task_id,
            str(proposal_revision),
            card_id,
            envelope.idempotency_key,
        )
        with self.commands.task_guard(task_id):
            existing = self.store.plan_proposal.get(task_id, next_proposal_id)
            if existing is not None:
                revised = next(
                    (card for card in existing["execution_cards"] if card["card_id"] == card_id),
                    None,
                )
                if revised is None or self._editable_card_fields(revised) != editable:
                    raise HarnessError(
                        "IDEMPOTENCY_CONFLICT",
                        "The task-card idempotency key was reused with different content.",
                    )
                return self._session(task_id)

            task = self._task(task_id)
            thread = self._ensure_thread(task_id, task)
            proposal = self._proposal_by_revision(task_id, proposal_revision)
            if thread["latest_proposal_revision"] != proposal_revision:
                self._revision_conflict(
                    "plan_proposal",
                    proposal["proposal_id"],
                    proposal_revision,
                    thread["latest_proposal_revision"],
                )
            if proposal["status"] not in {"PENDING_CONFIRMATION", "CONFIRMED"}:
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "Only the latest active plan may be revised.",
                )
            card_index = next(
                (
                    index
                    for index, card in enumerate(proposal["execution_cards"])
                    if card["card_id"] == card_id
                ),
                None,
            )
            if card_index is None:
                raise HarnessError("TASK_NOT_FOUND", "The selected TaskCard does not exist.")
            current_card = proposal["execution_cards"][card_index]
            if current_card["revision"] != expected_card_revision:
                self._revision_conflict(
                    "task_card",
                    card_id,
                    expected_card_revision,
                    current_card["revision"],
                )
            if not self._task_card_is_editable(task_id, proposal, current_card):
                raise HarnessError(
                    "INVALID_STATE_TRANSITION",
                    "A TaskCard cannot be revised after its instance has started.",
                    {"card_id": card_id, "instance_id": current_card["instance_id"]},
                )

            normalized_editable = deepcopy(editable)
            normalized_editable["input_assets"] = self._validate_asset_refs(
                task_id, normalized_editable["input_assets"]
            )
            now = utc_now()
            revised_card = {
                **deepcopy(current_card),
                **normalized_editable,
                "revision": current_card["revision"] + 1,
                "created_at": now,
            }
            adapter = self.adapters.get_optional(revised_card["agent_type"])
            if adapter is not None:
                validation = adapter.validate_task_card(cast(TaskCard, revised_card))
                if not validation.valid:
                    raise HarnessError(
                        "VALIDATION_ERROR",
                        "The Agent adapter rejected the revised task card.",
                        {"errors": list(validation.errors)},
                    )

            next_proposal = deepcopy(proposal)
            next_proposal.update(
                {
                    "proposal_id": next_proposal_id,
                    "revision": proposal_revision + 1,
                    "status": "PENDING_CONFIRMATION",
                    "created_at": now,
                    "updated_at": now,
                    "confirmed_at": None,
                }
            )
            next_proposal["execution_cards"][card_index] = revised_card
            self.plan_proposals.validate_new(
                task_id,
                next_proposal,
                expected_revision=proposal_revision + 1,
            )

            actor = Actor(envelope.actor_type, envelope.actor_id)
            self._put_plan_proposal(
                task_id,
                next_proposal,
                expected_revision=0,
                actor=actor,
                command="revise_plan_task_card",
                idempotency_key=envelope.idempotency_key,
            )
            superseded = {
                **proposal,
                "status": "SUPERSEDED",
                "updated_at": now,
                "confirmed_at": None,
            }
            self._put_plan_proposal(
                task_id,
                superseded,
                expected_revision=self.store.plan_proposal.revision(
                    task_id, proposal["proposal_id"]
                ),
                actor=actor,
                command="supersede_plan_proposal_after_card_revision",
                idempotency_key=self._identifier(
                    "supersede_card_edit", task_id, next_proposal_id
                ),
            )
            message = self._message(
                task_id,
                self._identifier("message_card_edit", task_id, next_proposal_id),
                thread["latest_sequence"] + 1,
                role="system",
                kind="plan_proposal",
                content=(
                    f"任务卡 {card_id} 已保存为 r{revised_card['revision']}, "
                    f"计划已更新为 r{next_proposal['revision']}, 请审阅未启动任务卡后继续。"
                ),
                asset_refs=[],
                created_at=now,
            )
            self._put_message(message, actor, "append_task_card_revision_message")
            self._put_thread(
                task_id,
                {
                    **thread,
                    "latest_sequence": message["sequence"],
                    "latest_proposal_revision": next_proposal["revision"],
                    "last_error": None,
                },
                actor,
                "publish_task_card_revision",
            )
            return self._session(task_id)

    @staticmethod
    def _require_human_confirmation(actor_type: object) -> None:
        if actor_type != "human":
            raise HarnessError(
                "VALIDATION_ERROR",
                "Only a human may confirm and start a Master plan.",
            )

    def _confirm(
        self,
        task_id: str,
        proposal_revision: int,
        *,
        task_expected_revision: int,
        expected_card_revisions: dict[str, int],
        envelope: CommandEnvelope,
        instance_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_human_confirmation(envelope.actor_type)
        self._require_task_not_archived(task_id)
        intent_path = self._confirm_intent_path(task_id, proposal_revision)
        with self.commands.task_guard(task_id):
            if intent_path.exists():
                intent = read_json(intent_path)
                if intent["state"] == "COMPLETED":
                    return deepcopy(intent["result"])
                if intent["state"] == "ABORTED":
                    self._raise_aborted_confirmation_intent(intent)
            else:
                task = self._task(task_id)
                self._ensure_thread(task_id, task)
                proposal = self._proposal_by_revision(task_id, proposal_revision)
                if envelope.expected_revision != proposal_revision:
                    self._revision_conflict(
                        "plan_proposal",
                        proposal["proposal_id"],
                        envelope.expected_revision,
                        proposal_revision,
                    )
                start_subset = None
                if instance_ids is not None:
                    known = {card["instance_id"] for card in proposal["execution_cards"]}
                    unknown = [
                        instance_id for instance_id in instance_ids if instance_id not in known
                    ]
                    if unknown:
                        raise HarnessError(
                            "VALIDATION_ERROR",
                            "Only instances of the selected plan may be started.",
                            {"instance_ids": unknown},
                        )
                    start_subset = sorted(set(instance_ids))
                    if not start_subset:
                        raise HarnessError(
                            "VALIDATION_ERROR",
                            "At least one instance must be selected to start.",
                        )
                intent = {
                    "schema_version": "1.0",
                    "kind": "CONFIRM_MASTER_PLAN",
                    "task_id": task_id,
                    "proposal_id": proposal["proposal_id"],
                    "proposal_revision": proposal_revision,
                    "expected_card_revisions": deepcopy(expected_card_revisions),
                    "task_expected_revision": task_expected_revision,
                    "instance_ids": start_subset,
                    "plan_mode": (
                        "merge"
                        if self.store.plan.get(task_id, task_id) is not None
                        else "replace"
                    ),
                    "actor": {
                        "actor_type": envelope.actor_type,
                        "actor_id": envelope.actor_id,
                    },
                    "state": "PREPARED",
                    "prepared_at": utc_now(),
                    "plan_result": None,
                    "start_result": None,
                    "result": None,
                }
                # Persist PREPARED before the source truth gate so a terminal
                # citation rejection is recoverable and auditable as ABORTED.
                self._validate_confirmation_preconditions(
                    intent,
                    task_expected_revision=task_expected_revision,
                )
                atomic_write_json(intent_path, intent)
            return self._resume_confirm(intent_path)

    def _resume_confirm(self, intent_path: Path) -> dict[str, Any]:
        intent = read_json(intent_path)
        if intent.get("state") == "ABORTED":
            self._raise_aborted_confirmation_intent(intent)
        try:
            actor = intent.get("actor")
            self._require_human_confirmation(
                actor.get("actor_type") if isinstance(actor, dict) else None
            )
        except HarnessError as exc:
            self._abort_confirmation_intent(intent_path, intent, exc)
            raise
        task_id = intent["task_id"]
        if intent["state"] == "PREPARED":
            proposal = self._validate_confirmation_gate_or_abort(
                intent_path,
                intent,
                task_expected_revision=intent.get("task_expected_revision"),
            )
            stages, instances, cards = materialize_plan_proposal(proposal)
            actor = intent["actor"]
            plan_result = self.application.save_plan_and_create_instances(
                task_id,
                stages=cast(list[StageSnapshot], stages),
                instances=cast(list[AgentInstanceSnapshot], instances),
                task_cards=cast(list[TaskCard], cards),
                operation_id=self._identifier("master_plan", task_id, proposal["proposal_id"]),
                # Intents created before per-card editing existed always represented
                # the first plan save, so a missing field safely falls back to replace.
                mode=intent.get("plan_mode", "replace"),
                envelope=CommandEnvelope(
                    idempotency_key=self._identifier(
                        "save_master_plan", task_id, proposal["proposal_id"]
                    ),
                    actor_type=actor["actor_type"],
                    actor_id=actor["actor_id"],
                    expected_revision=int(intent["task_expected_revision"]),
                ),
            )
            intent.update({"state": "PLAN_SAVED", "plan_result": plan_result})
            atomic_write_json(intent_path, intent)
        if intent["state"] == "PLAN_SAVED":
            plan_result = intent.get("plan_result")
            saved_task_revision = (
                plan_result.get("task_revision") if isinstance(plan_result, dict) else None
            )
            proposal = self._validate_confirmation_gate_or_abort(
                intent_path,
                intent,
                task_expected_revision=saved_task_revision,
            )
            actor = intent["actor"]
            start_result = self.application.confirm_and_start_ready_instances(
                task_id,
                operation_id=self._identifier("master_start", task_id, proposal["proposal_id"]),
                envelope=CommandEnvelope(
                    idempotency_key=self._identifier(
                        "start_master_plan", task_id, proposal["proposal_id"]
                    ),
                    actor_type=actor["actor_type"],
                    actor_id=actor["actor_id"],
                    expected_revision=cast(int, saved_task_revision),
                ),
                only_instance_ids=intent.get("instance_ids"),
            )
            intent.update({"state": "STARTED", "start_result": start_result})
            atomic_write_json(intent_path, intent)
        if intent["state"] == "STARTED":
            proposal_id = intent["proposal_id"]
            current = self.store.plan_proposal.get(task_id, proposal_id)
            if current is None:
                raise HarnessError("TASK_NOT_FOUND", "The selected PlanProposal does not exist.")
            if current["status"] != "CONFIRMED":
                now = utc_now()
                confirmed = {
                    **current,
                    "status": "CONFIRMED",
                    "updated_at": now,
                    "confirmed_at": now,
                }
                self._put_plan_proposal(
                    task_id,
                    confirmed,
                    expected_revision=self.store.plan_proposal.revision(
                        task_id, proposal_id
                    ),
                    actor=Actor(intent["actor"]["actor_type"], intent["actor"]["actor_id"]),
                    command="confirm_plan_proposal",
                    idempotency_key=self._identifier(
                        "confirm_proposal", task_id, proposal_id
                    ),
                )
            self._append_confirmation_message(task_id, current)
            result = {
                "schema_version": "1.0",
                "proposal": deepcopy(
                    self.store.plan_proposal.get(task_id, proposal_id)
                ),
                "plan_result": deepcopy(intent["plan_result"]),
                "start_result": deepcopy(intent["start_result"]),
                "session": self._session(task_id),
            }
            intent.update({
                "state": "COMPLETED",
                "completed_at": utc_now(),
                "result": result,
            })
            atomic_write_json(intent_path, intent)
        return deepcopy(intent["result"])

    def _validate_confirmation_gate_or_abort(
        self,
        intent_path: Path,
        intent: dict[str, Any],
        *,
        task_expected_revision: object,
    ) -> dict[str, Any]:
        try:
            return self._validate_confirmation_gate(
                intent, task_expected_revision=task_expected_revision
            )
        except HarnessError as exc:
            self._abort_confirmation_intent(intent_path, intent, exc)
            raise

    def _validate_confirmation_gate(
        self,
        intent: dict[str, Any],
        *,
        task_expected_revision: object,
    ) -> dict[str, Any]:
        proposal = self._validate_confirmation_preconditions(
            intent, task_expected_revision=task_expected_revision
        )
        self.plan_proposals.validate_sources(proposal["task_id"], proposal)
        return proposal

    def _validate_confirmation_preconditions(
        self,
        intent: dict[str, Any],
        *,
        task_expected_revision: object,
    ) -> dict[str, Any]:
        task_id = intent.get("task_id")
        proposal_id = intent.get("proposal_id")
        proposal_revision = intent.get("proposal_revision")
        expected_card_revisions = intent.get("expected_card_revisions")
        if (
            not isinstance(task_id, str)
            or not isinstance(proposal_id, str)
            or not isinstance(proposal_revision, int)
            or not isinstance(task_expected_revision, int)
            or not isinstance(expected_card_revisions, dict)
            or not all(
                isinstance(card_id, str) and isinstance(revision, int)
                for card_id, revision in expected_card_revisions.items()
            )
        ):
            raise HarnessError("VALIDATION_ERROR", "A Master confirmation intent is invalid.")

        proposal = self.store.plan_proposal.get(task_id, proposal_id)
        if proposal is None:
            raise HarnessError("TASK_NOT_FOUND", "The selected PlanProposal does not exist.")
        if proposal.get("revision") != proposal_revision:
            actual_revision = proposal.get("revision")
            if not isinstance(actual_revision, int):
                raise HarnessError(
                    "VALIDATION_ERROR", "The selected PlanProposal revision is invalid."
                )
            self._revision_conflict(
                "plan_proposal", proposal_id, proposal_revision, actual_revision
            )

        thread = self.store.master_thread.get(task_id, task_id)
        if thread is None:
            raise HarnessError("TASK_NOT_FOUND", "The Master thread does not exist.")
        latest_revision = thread["latest_proposal_revision"]
        if latest_revision != proposal_revision:
            self._revision_conflict(
                "plan_proposal", proposal_id, proposal_revision, latest_revision
            )
        latest_proposal = self._proposal_by_revision(task_id, latest_revision)
        if latest_proposal["proposal_id"] != proposal_id:
            raise HarnessError(
                "REVISION_CONFLICT",
                "The selected PlanProposal is no longer the latest proposal.",
                {
                    "object_type": "plan_proposal",
                    "object_id": proposal_id,
                    "expected_proposal_id": proposal_id,
                    "actual_proposal_id": latest_proposal["proposal_id"],
                },
            )
        if proposal["status"] != "PENDING_CONFIRMATION":
            raise HarnessError(
                "INVALID_STATE_TRANSITION", "Only the latest pending plan may run."
            )

        actual_card_revisions = {
            card["card_id"]: card["revision"] for card in proposal["execution_cards"]
        }
        if expected_card_revisions != actual_card_revisions:
            raise HarnessError(
                "REVISION_CONFLICT",
                "A TaskCard revision changed before this plan was confirmed.",
                {
                    "object_type": "task_card_set",
                    "object_id": proposal_id,
                    "expected_revisions": deepcopy(expected_card_revisions),
                    "actual_revisions": actual_card_revisions,
                },
            )
        actual_task_revision = self.store.task.revision(task_id, task_id)
        if task_expected_revision != actual_task_revision:
            self._revision_conflict(
                "task", task_id, task_expected_revision, actual_task_revision
            )
        return proposal

    @staticmethod
    def _abort_confirmation_intent(
        intent_path: Path, intent: dict[str, Any], error: HarnessError
    ) -> None:
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

    @staticmethod
    def _raise_aborted_confirmation_intent(intent: dict[str, Any]) -> None:
        error = intent["error"]
        raise HarnessError(error["code"], error["message"], deepcopy(error["details"]))

    def _ensure_intake_message(
        self, task_id: str, task: dict[str, Any], thread: dict[str, Any]
    ) -> dict[str, Any]:
        if thread["latest_sequence"] > 0:
            return thread
        intake = self.store.task_intake.get(task_id, task_id)
        if intake is None or intake["status"] != "SUBMITTED":
            return thread
        message_id = self._identifier("message_intake", task_id)
        refs = []
        for asset_id in intake["asset_ids"]:
            manifest = self.assets.verify_asset(task_id, asset_id)
            refs.append(
                {
                    "asset_id": asset_id,
                    "manifest_relpath": (
                        f"inputs/manifests/{asset_id}.json"
                        if manifest["producer_instance_id"] is None
                        else f"resources/manifests/{asset_id}.json"
                    ),
                }
            )
        message = self._message(
            task_id,
            message_id,
            1,
            role="user",
            kind="text",
            content=intake["prompt"],
            asset_refs=refs,
            created_at=intake["submitted_at"],
        )
        actor = Actor("system", "intake_bridge")
        if self.store.master_message.get(task_id, message_id) is None:
            self._put_message(message, actor, "seed_master_thread_from_intake")
        now = utc_now()
        thread = self._put_thread(
            task_id,
            {
                **thread,
                "latest_sequence": 1,
                "active_run": {
                    "run_id": None,
                    "message_id": message_id,
                    "status": "SUBMITTING",
                    "started_at": now,
                    "updated_at": now,
                },
            },
            actor,
            "queue_intake_master_run",
        )
        return thread

    def _advance_active_run(self, task_id: str, thread: dict[str, Any]) -> dict[str, Any]:
        active = thread["active_run"]
        if active is None:
            return thread
        try:
            if active["status"] == "SUBMITTING":
                message = self.store.master_message.get(task_id, active["message_id"])
                if message is None:
                    raise HarnessError("TASK_NOT_FOUND", "The queued Master message is missing.")
                run_id = self.orchestrator.submit_message(task_id, message)
                now = utc_now()
                thread = self._put_thread(
                    task_id,
                    {
                        **thread,
                        "active_run": {
                            **active,
                            "run_id": run_id,
                            "status": "RUNNING",
                            "updated_at": now,
                        },
                        "last_error": None,
                    },
                    Actor("master", "master_orchestrator"),
                    "submit_master_run",
                )
                active = thread["active_run"]
            if active["status"] != "RUNNING" or active["run_id"] is None:
                return thread
            observation = self.orchestrator.observe_run(task_id, active["run_id"])
            if observation.status in {"QUEUED", "RUNNING"}:
                return thread
            if observation.status == "NEEDS_INPUT":
                content = (observation.message or "Master 需要补充信息后才能继续。").strip()
                return self._append_master_message(
                    task_id,
                    thread,
                    active,
                    role="master",
                    kind="clarification",
                    content=content,
                    target_status="NEEDS_INPUT",
                    command="record_master_clarification",
                )
            if observation.status == "FAILED":
                raise MasterOrchestratorFailure(
                    observation.error_code or "MASTER_RUN_FAILED",
                    (observation.message or "Master 计划运行失败, 请补充要求后重试。").strip(),
                )
            return self._store_orchestrated_plan(task_id, thread, active, observation)
        except MasterOrchestratorFailure as exc:
            return self._record_orchestrator_error(
                task_id, thread, active, exc.code, exc.message
            )

    def _store_orchestrated_plan(
        self,
        task_id: str,
        thread: dict[str, Any],
        active: dict[str, Any],
        observation: MasterRunObservation,
    ) -> dict[str, Any]:
        proposal = self.orchestrator.load_plan(task_id, active["run_id"])
        expected_revision = thread["latest_proposal_revision"] + 1
        self.plan_proposals.validate_new(
            task_id,
            proposal,
            expected_revision=expected_revision,
        )
        actor = Actor("master", "master_orchestrator")
        existing = self.store.plan_proposal.get(task_id, proposal["proposal_id"])
        if existing is None:
            self._put_plan_proposal(
                task_id,
                deepcopy(proposal),
                expected_revision=0,
                actor=actor,
                command="save_plan_proposal",
                idempotency_key=self._identifier("save_proposal", task_id, proposal["proposal_id"]),
            )
        elif existing != proposal:
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT", "Master reused a proposal id with different content."
            )
        for previous in self.store.plan_proposal.list(task_id):
            if (
                previous["proposal_id"] != proposal["proposal_id"]
                and previous["status"] == "PENDING_CONFIRMATION"
            ):
                updated = {
                    **previous,
                    "status": "SUPERSEDED",
                    "updated_at": utc_now(),
                    "confirmed_at": None,
                }
                self._put_plan_proposal(
                    task_id,
                    updated,
                    expected_revision=self.store.plan_proposal.revision(
                        task_id, previous["proposal_id"]
                    ),
                    actor=actor,
                    command="supersede_plan_proposal",
                    idempotency_key=self._identifier(
                        "supersede_proposal",
                        task_id,
                        previous["proposal_id"],
                        proposal["proposal_id"],
                    ),
                )
        default_content = f"已生成计划方案 r{proposal['revision']}, 请审阅。"
        content = (observation.message or default_content).strip()
        message_id = self._identifier("message_plan", task_id, proposal["proposal_id"])
        existing_message = self.store.master_message.get(task_id, message_id)
        if existing_message is None:
            message = self._message(
                task_id,
                message_id,
                thread["latest_sequence"] + 1,
                role="master",
                kind="plan_proposal",
                content=content,
                asset_refs=[],
            )
            self._put_message(message, actor, "append_master_plan_message")
            latest_sequence = message["sequence"]
        else:
            latest_sequence = max(
                thread["latest_sequence"],
                existing_message["sequence"],
            )
        now = utc_now()
        # Apply the generated title before publishing PLAN_READY: lock-free session
        # readers must never observe the terminal status with a stale task title.
        self._apply_generated_title(task_id, observation.task_title, proposal["proposal_id"])
        thread = self._put_thread(
            task_id,
            {
                **thread,
                "latest_sequence": latest_sequence,
                "latest_proposal_revision": proposal["revision"],
                "active_run": {**active, "status": "PLAN_READY", "updated_at": now},
                "last_error": None,
            },
            actor,
            "publish_master_plan",
        )
        return self.store.master_thread.get(task_id, task_id) or thread

    def _append_master_message(
        self,
        task_id: str,
        thread: dict[str, Any],
        active: dict[str, Any],
        *,
        role: str,
        kind: str,
        content: str,
        target_status: str,
        command: str,
    ) -> dict[str, Any]:
        message_id = self._identifier("message_run", task_id, active["run_id"], target_status)
        existing = self.store.master_message.get(task_id, message_id)
        if existing is None:
            message = self._message(
                task_id,
                message_id,
                thread["latest_sequence"] + 1,
                role=role,
                kind=kind,
                content=content,
                asset_refs=[],
            )
            self._put_message(message, Actor("master", "master_orchestrator"), command)
            latest_sequence = message["sequence"]
        else:
            latest_sequence = max(thread["latest_sequence"], existing["sequence"])
        return self._put_thread(
            task_id,
            {
                **thread,
                "latest_sequence": latest_sequence,
                "active_run": {
                    **active,
                    "status": target_status,
                    "updated_at": utc_now(),
                },
                "last_error": None,
            },
            Actor("master", "master_orchestrator"),
            command,
        )

    def _record_orchestrator_error(
        self,
        task_id: str,
        thread: dict[str, Any],
        active: dict[str, Any] | None,
        code: str,
        message: str,
    ) -> dict[str, Any]:
        safe_message = message.strip()[:2000] or "Master 服务暂时不可用。"
        now = utc_now()
        error_id = self._identifier(
            "message_error", task_id, None if active is None else active["message_id"], code
        )
        existing = self.store.master_message.get(task_id, error_id)
        latest_sequence = thread["latest_sequence"]
        if existing is None:
            error_message = self._message(
                task_id,
                error_id,
                latest_sequence + 1,
                role="system",
                kind="error",
                content=safe_message,
                asset_refs=[],
            )
            self._put_message(
                error_message,
                Actor("system", "master_orchestrator_boundary"),
                "record_master_error",
            )
            latest_sequence = error_message["sequence"]
        else:
            latest_sequence = max(latest_sequence, existing["sequence"])
        return self._put_thread(
            task_id,
            {
                **thread,
                "latest_sequence": latest_sequence,
                "active_run": (
                    None
                    if active is None
                    else {**active, "status": "FAILED", "updated_at": now}
                ),
                "last_error": {"code": code, "message": safe_message, "occurred_at": now},
            },
            Actor("system", "master_orchestrator_boundary"),
            "record_master_error",
        )

    def _append_confirmation_message(
        self, task_id: str, proposal: dict[str, Any]
    ) -> None:
        message_id = self._identifier("message_confirm", task_id, proposal["proposal_id"])
        if self.store.master_message.get(task_id, message_id) is not None:
            return
        thread = self.store.master_thread.get(task_id, task_id)
        if thread is None:
            raise HarnessError("TASK_NOT_FOUND", "The Master thread does not exist.")
        message = self._message(
            task_id,
            message_id,
            thread["latest_sequence"] + 1,
            role="system",
            kind="plan_confirmation",
            content=f"计划 r{proposal['revision']} 已确认, 符合门禁的实例已进入启动流程。",
            asset_refs=[],
        )
        actor = Actor("system", "master_plan_confirmation")
        self._put_message(message, actor, "append_plan_confirmation_message")
        self._put_thread(
            task_id,
            {**thread, "latest_sequence": message["sequence"], "last_error": None},
            actor,
            "complete_plan_confirmation",
        )

    def _apply_generated_title(
        self, task_id: str, raw_title: str | None, proposal_id: str
    ) -> None:
        if raw_title is None:
            return
        title = " ".join(raw_title.split())
        if not title or len(title) > 256:
            raise HarnessError("VALIDATION_ERROR", "Master generated an invalid task title.")
        task = self._task(task_id)
        if task["title"] == title:
            return
        self.commands.rename_task(
            task_id,
            title,
            CommandEnvelope(
                idempotency_key=self._identifier("master_title", task_id, proposal_id),
                actor_type="master",
                actor_id="master_orchestrator",
                expected_revision=self.store.task.revision(task_id, task_id),
            ),
        )

    def _validate_asset_refs(
        self, task_id: str, raw_refs: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        if len(raw_refs) > 20:
            raise HarnessError("VALIDATION_ERROR", "Too many asset references.")
        refs: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in raw_refs:
            asset_id = raw.get("asset_id", "")
            if asset_id in seen:
                raise HarnessError("VALIDATION_ERROR", "Asset references must be unique.")
            manifest = self.assets.verify_asset(task_id, asset_id)
            expected = (
                f"inputs/manifests/{asset_id}.json"
                if manifest["producer_instance_id"] is None
                else f"resources/manifests/{asset_id}.json"
            )
            if raw.get("manifest_relpath") != expected:
                raise HarnessError(
                    "VALIDATION_ERROR",
                    "An asset reference does not use its authoritative manifest.",
                )
            seen.add(asset_id)
            refs.append({"asset_id": asset_id, "manifest_relpath": expected})
        return refs

    @staticmethod
    def _editable_card_fields(card: dict[str, Any]) -> dict[str, Any]:
        return {
            "objective": deepcopy(card["objective"]),
            "instructions": deepcopy(card["instructions"]),
            "input_assets": deepcopy(card["input_assets"]),
            "expected_deliveries": deepcopy(card["expected_deliveries"]),
            "parameters": deepcopy(card["parameters"]),
        }

    def _task_card_is_editable(
        self,
        task_id: str,
        proposal: dict[str, Any],
        card: dict[str, Any],
    ) -> bool:
        instance = self.store.instance.get(task_id, card["instance_id"])
        if instance is None:
            return proposal["status"] == "PENDING_CONFIRMATION"
        if instance["status"] not in _EDITABLE_INSTANCE_STATUSES:
            return False
        latest_start = getattr(self.application, "latest_start_operation", None)
        if callable(latest_start):
            return latest_start(task_id, instance_id=card["instance_id"]) is None
        return True

    def _session(self, task_id: str) -> dict[str, Any]:
        task = self._task(task_id)
        thread = self.store.master_thread.get(task_id, task_id)
        if thread is None:
            thread = self._ensure_thread(task_id, task)
        messages = self.store.master_message.list(task_id)
        messages.sort(key=lambda item: (item["sequence"], item["created_at"], item["message_id"]))
        proposals = self.store.plan_proposal.list(task_id)
        latest = max(proposals, key=lambda item: item["revision"], default=None)
        instances = self.store.instance.list(task_id)
        instance_by_id = {instance["instance_id"]: instance for instance in instances}
        instance_statuses = {
            instance_id: instance["status"] for instance_id, instance in instance_by_id.items()
        }
        unfinished_image_instance_ids = [
            instance["instance_id"]
            for instance in instances
            if instance["agent_type"] == "image"
            and instance["status"]
            not in {"SUCCEEDED", "SKIPPED", "SUPERSEDED", "ARCHIVED"}
            and not instance.get("manual_finished", False)
        ]
        if latest is not None:
            unfinished_image_instance_ids.extend(
                card["instance_id"]
                for card in latest["execution_cards"]
                if card["agent_type"] == "image"
                and card["instance_id"] not in instance_by_id
            )
        editable_card_ids = (
            []
            if latest is None
            else [
                card["card_id"]
                for card in latest["execution_cards"]
                if self._task_card_is_editable(task_id, latest, card)
            ]
        )
        message_ids = {message["message_id"] for message in messages}
        proposal_entries = [
            {
                **deepcopy(proposal),
                "message_id": self._proposal_message_id(
                    task_id, proposal["proposal_id"], message_ids
                ),
            }
            for proposal in sorted(proposals, key=lambda item: item["revision"])
        ]
        intake = self.store.task_intake.get(task_id, task_id)
        assets = []
        if intake is not None:
            for asset_id in intake["asset_ids"]:
                manifest = self.assets.verify_asset(task_id, asset_id)
                assets.append(
                    {
                        "asset_id": asset_id,
                        "filename": Path(manifest["relative_path"]).name,
                        "description": manifest["description"],
                        "manifest_relpath": f"inputs/manifests/{asset_id}.json",
                    }
                )
        return {
            "schema_version": "1.0",
            "thread": deepcopy(thread),
            "thread_revision": self.store.master_thread.revision(task_id, task_id),
            "messages": deepcopy(messages),
            "latest_proposal": deepcopy(latest),
            "proposals": proposal_entries,
            "editable_card_ids": editable_card_ids,
            "instance_statuses": instance_statuses,
            "unfinished_image_instance_ids": unfinished_image_instance_ids,
            "task": deepcopy(task),
            "task_revision": self.store.task.revision(task_id, task_id),
            # Kept until the designer-shell cleanup stage removes the old response field.
            "gateway_available": True,
            "assets": assets,
        }

    def _proposal_message_id(
        self, task_id: str, proposal_id: str, message_ids: set[str]
    ) -> str | None:
        for prefix in ("message_plan", "message_card_edit"):
            candidate = self._identifier(prefix, task_id, proposal_id)
            if candidate in message_ids:
                return candidate
        return None

    def _ensure_thread(
        self, task_id: str, task: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        current = self.store.master_thread.get(task_id, task_id)
        if current is not None:
            return current
        task = task or self._task(task_id)
        now = utc_now()
        thread = {
            "schema_version": "1.0",
            "task_id": task_id,
            "latest_sequence": 0,
            "latest_proposal_revision": 0,
            "active_run": None,
            "last_error": None,
            "revision": 1,
            "created_at": task["created_at"],
            "updated_at": now,
        }
        self.store.master_thread.put(
            task_id,
            task_id,
            thread,
            expected_revision=0,
            actor=Actor("system", "master_thread_initializer"),
            command="create_master_thread",
            idempotency_key=self._identifier("create_master_thread", task_id),
        )
        return thread

    def _put_thread(
        self,
        task_id: str,
        thread: dict[str, Any],
        actor: Actor,
        command: str,
    ) -> dict[str, Any]:
        actual = self.store.master_thread.revision(task_id, task_id)
        updated = {
            **deepcopy(thread),
            "revision": actual + 1,
            "updated_at": utc_now(),
        }
        self.store.master_thread.put(
            task_id,
            task_id,
            updated,
            expected_revision=actual,
            actor=actor,
            command=command,
            idempotency_key=self._identifier(command, task_id, str(actual + 1)),
        )
        return updated

    def _put_message(self, message: dict[str, Any], actor: Actor, command: str) -> None:
        self.store.master_message.put(
            message["task_id"],
            message["message_id"],
            deepcopy(message),
            expected_revision=0,
            actor=actor,
            command=command,
            idempotency_key=self._identifier(command, message["task_id"], message["message_id"]),
        )

    def _put_plan_proposal(
        self,
        task_id: str,
        proposal: dict[str, Any],
        *,
        expected_revision: int,
        actor: Actor,
        command: str,
        idempotency_key: str,
    ) -> None:
        """Apply the shared citation truth gate to every proposal repository write."""

        self.plan_proposals.validate_sources(task_id, proposal)
        self.store.plan_proposal.put(
            task_id,
            proposal["proposal_id"],
            proposal,
            expected_revision=expected_revision,
            actor=actor,
            command=command,
            idempotency_key=idempotency_key,
        )

    def _proposal_by_revision(self, task_id: str, revision: int) -> dict[str, Any]:
        matches = [
            item for item in self.store.plan_proposal.list(task_id) if item["revision"] == revision
        ]
        if len(matches) != 1:
            raise HarnessError("TASK_NOT_FOUND", "The requested PlanProposal does not exist.")
        return matches[0]

    def _task(self, task_id: str) -> dict[str, Any]:
        task = self.store.task.get(task_id, task_id)
        if task is None:
            raise HarnessError("TASK_NOT_FOUND", "The requested task does not exist.")
        return deepcopy(task)

    def _require_task_not_archived(self, task_id: str) -> None:
        if self._task(task_id).get("status") == "ARCHIVED":
            raise HarnessError(
                "TASK_ARCHIVED",
                "The task is archived and read-only; resume it before issuing changes.",
                {"task_id": task_id},
            )

    def _confirm_intent_path(self, task_id: str, revision: int) -> Path:
        return self.intent_root / f"{self._identifier('confirm', task_id, str(revision))}.json"

    @staticmethod
    def _message(
        task_id: str,
        message_id: str,
        sequence: int,
        *,
        role: str,
        kind: str,
        content: str,
        asset_refs: list[dict[str, str]],
        created_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "message_id": message_id,
            "task_id": task_id,
            "sequence": sequence,
            "role": role,
            "kind": kind,
            "content": content,
            "asset_refs": deepcopy(asset_refs),
            "created_at": created_at or utc_now(),
        }

    @staticmethod
    def _identifier(prefix: str, *parts: object) -> str:
        digest = hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()
        return f"{prefix}_{digest[:32]}"

    @staticmethod
    def _revision_conflict(
        object_type: str, object_id: str, expected: int, actual: int
    ) -> None:
        raise HarnessError(
            "REVISION_CONFLICT",
            "The object revision changed before this command committed.",
            {
                "object_type": object_type,
                "object_id": object_id,
                "expected_revision": expected,
                "actual_revision": actual,
            },
        )
