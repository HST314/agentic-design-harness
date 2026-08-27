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
from .ppt import PptAgentAdapter
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
    "PptAgentAdapter",
    "PrepareRequest",
    "StageSnapshot",
    "TaskCard",
    "UsageEvent",
    "ValidationResult",
]
