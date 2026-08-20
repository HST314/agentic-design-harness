"""Closed, typed registry for professional Agent adapters."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ..core.errors import HarnessError
from .base import AgentAdapter

_AGENT_TYPE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class AdapterRegistry:
    def __init__(self, adapters: Iterable[AgentAdapter] = ()) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: AgentAdapter) -> None:
        if not isinstance(adapter, AgentAdapter) or not _AGENT_TYPE.fullmatch(
            adapter.agent_type
        ):
            raise HarnessError("VALIDATION_ERROR", "The Agent adapter is invalid.")
        if adapter.agent_type in self._adapters:
            raise HarnessError(
                "IDEMPOTENCY_CONFLICT",
                "Only one adapter may own an Agent type.",
                {"agent_type": adapter.agent_type},
            )
        self._adapters[adapter.agent_type] = adapter

    def get(self, agent_type: str) -> AgentAdapter:
        adapter = self._adapters.get(agent_type)
        if adapter is None:
            raise HarnessError(
                "ADAPTER_UNAVAILABLE",
                "No adapter is registered for the requested Agent type.",
                {"agent_type": agent_type},
            )
        return adapter

    def get_optional(self, agent_type: str) -> AgentAdapter | None:
        return self._adapters.get(agent_type)

    def describe(self) -> list[dict[str, object]]:
        return [
            {"agent_type": name, "available": adapter.available}
            for name, adapter in sorted(self._adapters.items())
        ]
