"""Phase 1 contract placeholder for the intentionally unavailable PPT Agent."""

from __future__ import annotations

from typing import Any, NoReturn

from ..core.errors import HarnessError
from ..services.process_runtime import ProcessSpec
from .base import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    PrepareRequest,
    ValidationResult,
)
from .types import AgentInstanceSnapshot, DeliveryCandidate, TaskCard, UsageEvent


class PptAgentContractAdapter:
    agent_type = "ppt"
    available = False

    def validate_task_card(self, card: TaskCard) -> ValidationResult:
        errors = () if card.get("agent_type") == self.agent_type else (
            "Task card agent_type must be ppt.",
        )
        return ValidationResult(valid=not errors, errors=errors)

    def prepare(self, request: PrepareRequest) -> ProcessSpec:
        self._unavailable(request.instance.get("instance_id"))

    def start(self, instance_id: str, operation_id: str) -> AdapterCommandResult:
        self._unavailable(instance_id)

    def stop(
        self, instance_id: str, reason: str, operation_id: str
    ) -> AdapterCommandResult:
        self._unavailable(instance_id)

    def get_status(self, instance_id: str) -> AdapterObservation:
        return AdapterObservation(
            status="UNAVAILABLE",
            details={"instance_id": instance_id, "reason": "PPT Agent is not connected."},
        )

    def request_advance(
        self,
        instance_id: str,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> AdapterCommandResult:
        self._unavailable(instance_id)

    def collect_deliveries(self, instance_id: str) -> list[DeliveryCandidate]:
        self._unavailable(instance_id)

    def collect_usage(
        self, instance_id: str, cursor: str | None
    ) -> list[UsageEvent]:
        self._unavailable(instance_id)

    def get_ui_url(self, instance_id: str) -> str | None:
        return None

    def validate_ui_url(
        self, instance: AgentInstanceSnapshot, ui_url: str
    ) -> ValidationResult:
        return ValidationResult(False, ("The PPT workbench is unavailable.",))

    def recover(self, instance_snapshot: AgentInstanceSnapshot) -> AdapterRecoveryResult:
        return AdapterRecoveryResult(recovered=True, status="UNAVAILABLE")

    @staticmethod
    def _unavailable(instance_id: object) -> NoReturn:
        raise HarnessError(
            "ADAPTER_UNAVAILABLE",
            "PPT Agent is not available in Phase 1.",
            {"agent_type": "ppt", "instance_id": instance_id},
        )
