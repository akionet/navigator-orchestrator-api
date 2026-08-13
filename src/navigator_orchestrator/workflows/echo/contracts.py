"""Edge contracts for `echo` (SPEC-AIP-002 §3.2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["EchoInput", "EchoOutput"]


class EchoInput(BaseModel):
    """`extra="forbid"` so `{"wrong": 1}` is a 422, not a silently-ignored key."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4096)


class EchoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    model: str
    tokens: int = Field(ge=0)
