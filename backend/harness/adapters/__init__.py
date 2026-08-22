"""Typed professional-Agent integration boundary."""

from .base import (
    AdapterCommandResult,
    AdapterObservation,
    AdapterRecoveryResult,
    AgentAdapter,
    PrepareRequest,
    ValidationResult,
)
from .image import ImageAgentAdapter
from .ppt import PptAgentContractAdapter
from .registry import AdapterRegistry
from .types import (
    AgentInstanceSnapshot,
    DeliveryCandidate,
    StageSnapshot,
    TaskCard,
    UsageEvent,
)

__all__ = [
    "AdapterCommandResult",
    "AdapterObservation",
    "AdapterRecoveryResult",
    "AdapterRegistry",
    "AgentAdapter",
    "AgentInstanceSnapshot",
    "DeliveryCandidate",
    "ImageAgentAdapter",
    "PptAgentContractAdapter",
    "PrepareRequest",
    "StageSnapshot",
    "TaskCard",
    "UsageEvent",
    "ValidationResult",
]
