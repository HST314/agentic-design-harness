"""Explicit values and Protocol shared by every Agent adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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
    credential_ref: tuple[str, int]


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

    def apply_config(
        self,
        instance_id: str,
        config: dict[str, Any],
        revision: int,
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
