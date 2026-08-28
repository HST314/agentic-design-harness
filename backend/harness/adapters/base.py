"""Explicit values and Protocol shared by every Agent adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn, Protocol, runtime_checkable

from ..core.errors import HarnessError
from ..services.process_runtime import ProcessSpec
from .types import AgentInstanceSnapshot, DeliveryCandidate, TaskCard, UsageEvent


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PrepareRequest:
    instance: AgentInstanceSnapshot
    task_card: TaskCard
    task_root: Path
    config_ref: Path


@dataclass(frozen=True, slots=True)
class AdapterCommandResult:
    accepted: bool
    operation_id: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterObservation:
    status: str
    step_id: str | None = None
    capabilities: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AdapterRecoveryResult:
    recovered: bool
    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UnavailableAgentAdapter:
    """Keep one failed capability visible without taking down the control plane."""

    agent_type: str
    cause: HarnessError
    action: str
    available: bool = field(default=False, init=False)

    @property
    def availability_message(self) -> str:
        return self.cause.message

    def validate_task_card(self, card: TaskCard) -> ValidationResult:
        del card
        return ValidationResult(False, (self.availability_message,))

    def prepare(self, request: PrepareRequest) -> ProcessSpec:
        self._raise(request.instance.get("instance_id"))

    def start(self, instance_id: str, operation_id: str) -> AdapterCommandResult:
        del operation_id
        self._raise(instance_id)

    def stop(
        self, instance_id: str, reason: str, operation_id: str
    ) -> AdapterCommandResult:
        del reason, operation_id
        self._raise(instance_id)

    def get_status(self, instance_id: str) -> AdapterObservation:
        self._raise(instance_id)

    def request_advance(
        self,
        instance_id: str,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> AdapterCommandResult:
        del action, payload, operation_id
        self._raise(instance_id)

    def collect_deliveries(self, instance_id: str) -> list[DeliveryCandidate]:
        self._raise(instance_id)

    def collect_usage(
        self, instance_id: str, cursor: str | None
    ) -> list[UsageEvent]:
        del cursor
        self._raise(instance_id)

    def get_ui_url(self, instance_id: str) -> str | None:
        del instance_id
        return None

    def validate_ui_url(
        self, instance: AgentInstanceSnapshot, ui_url: str
    ) -> ValidationResult:
        del instance, ui_url
        return ValidationResult(False, (self.availability_message,))

    def recover(self, instance_snapshot: AgentInstanceSnapshot) -> AdapterRecoveryResult:
        del instance_snapshot
        return AdapterRecoveryResult(
            recovered=True,
            status="UNAVAILABLE",
            details=self._details(),
        )

    def _details(self) -> dict[str, str]:
        return {
            "agent_type": self.agent_type,
            "reason": self.availability_message,
            "action": self.action,
            "error_code": self.cause.code,
        }

    def _raise(self, instance_id: object) -> NoReturn:
        raise HarnessError(
            "ADAPTER_UNAVAILABLE",
            self.availability_message,
            {"instance_id": instance_id, **self._details()},
        )


@runtime_checkable
class AgentAdapter(Protocol):
    """The only interface through which orchestration observes an Agent type."""

    agent_type: str
    available: bool

    def validate_task_card(self, card: TaskCard) -> ValidationResult: ...

    def prepare(self, request: PrepareRequest) -> ProcessSpec: ...

    def start(self, instance_id: str, operation_id: str) -> AdapterCommandResult: ...

    def stop(
        self, instance_id: str, reason: str, operation_id: str
    ) -> AdapterCommandResult: ...

    def get_status(self, instance_id: str) -> AdapterObservation: ...

    def request_advance(
        self,
        instance_id: str,
        action: str,
        payload: dict[str, Any],
        operation_id: str,
    ) -> AdapterCommandResult: ...

    def collect_deliveries(self, instance_id: str) -> list[DeliveryCandidate]: ...

    def collect_usage(
        self, instance_id: str, cursor: str | None
    ) -> list[UsageEvent]: ...

    def get_ui_url(self, instance_id: str) -> str | None: ...

    def validate_ui_url(
        self, instance: AgentInstanceSnapshot, ui_url: str
    ) -> ValidationResult: ...

    def recover(self, instance_snapshot: AgentInstanceSnapshot) -> AdapterRecoveryResult: ...
