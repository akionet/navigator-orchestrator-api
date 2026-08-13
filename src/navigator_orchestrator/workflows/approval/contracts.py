"""Edge contracts for `approval` (SPEC-AIP-003 §3.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from navigator_orchestrator.store import Verdict

__all__ = ["ApprovalInput", "ApprovalOutput"]


class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4096)


class ApprovalOutput(BaseModel):
    """What the run produced *and* what the human did with it.

    The decision appears here for the caller's convenience; the audit record
    is the decision chain in the store, not this.
    """

    model_config = ConfigDict(extra="forbid")

    proposal: str
    verdict: Verdict
    comment: str | None = None
    decided_by: str
