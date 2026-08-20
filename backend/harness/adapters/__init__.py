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

__all__ = [
    "AdapterCommandResult",
    "AdapterObservation",
    "AdapterRecoveryResult",
    "AdapterRegistry",
    "AgentAdapter",
    "ImageAgentAdapter",
    "PptAgentContractAdapter",
    "PrepareRequest",
    "ValidationResult",
]
