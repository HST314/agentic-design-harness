"""Uniform command envelope required by every mutating domain operation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    actor_type: Literal["human", "master", "system", "adapter"]
    actor_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
    expected_revision: int = Field(ge=0)
