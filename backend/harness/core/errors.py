"""Stable control-plane errors backed by the v1 error catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ErrorDefinition:
    code: str
    http_status: int
    retryable: bool


class ErrorCatalog:
    """Load stable public error properties from the frozen contract catalog."""

    def __init__(self, catalog_path: Path) -> None:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        self._definitions = {
            item["code"]: ErrorDefinition(
                code=item["code"],
                http_status=item["http_status"],
                retryable=item["retryable"],
            )
            for item in raw["errors"]
        }

    def get(self, code: str) -> ErrorDefinition:
        return self._definitions.get(code, self._definitions["INTERNAL_ERROR"])


@dataclass(slots=True)
class HarnessError(Exception):
    """An expected failure that is safe to serialize through the API."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class SimulatedCrash(BaseException):
    """Test-only abrupt interruption at a named durable commit point."""

    def __init__(self, checkpoint: str) -> None:
        super().__init__(checkpoint)
        self.checkpoint = checkpoint
