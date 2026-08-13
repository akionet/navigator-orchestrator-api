"""Record shapes (SPEC-AIP-003 §3.3).

Deliberately *not* stored here: the run's input payload. The checkpointer
already holds it, and copying user content into a second table doubles the PII
surface for no gain (§5.2).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TERMINAL_STATES",
    "DecisionRecord",
    "Principal",
    "RunRecord",
    "RunState",
    "Verdict",
]

#: `queued` is the worker's entry point (`DESIGN-WRK-001` §3.2): the API creates
#: a run it cannot execute, and a worker claims it. It is first in the list
#: because it is first in the lifecycle.
RunState = Literal["queued", "running", "awaiting_decision", "completed", "failed", "cancelled"]
Verdict = Literal["approve", "reject", "amend"]

#: States from which a run will never move on its own.
TERMINAL_STATES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


def _now() -> datetime:
    return datetime.now(UTC)


class Principal(BaseModel):
    """Who acted. An audit record without one is not an audit record (§3.5)."""

    model_config = ConfigDict(frozen=True)

    subject: str
    scopes: frozenset[str] = frozenset()
    #: Where the identity came from — `stub`, `header`, … Recorded so a later
    #: reader can tell a real principal from a development one.
    issuer: str = "stub"

    @property
    def is_anonymous(self) -> bool:
        return self.subject == "anonymous"


class RunRecord(BaseModel):
    """One workflow run. `id` is also the checkpointer `thread_id` (§3.2)."""

    model_config = ConfigDict(frozen=True)

    id: str
    workflow: str
    state: RunState = "running"
    #: `Policy.fingerprint()` — enough to reproduce the run, not the content.
    policy: dict[str, Any] = Field(default_factory=dict)
    #: Whatever the workflow passed to `interrupt()`. Opaque to the engine:
    #: the reviewer UI renders it, we never interpret it.
    gate_payload: dict[str, Any] | None = None
    created_by: Principal | None = None
    #: Which worker holds this run, and until when (`DESIGN-WRK-001` §4).
    #: A claim is a *lease*, not a transfer: a worker that dies mid-run stops
    #: renewing and the run returns to the queue on its own. The worker is not
    #: asked to notice its own death.
    leased_by: str | None = None
    lease_expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class DecisionRecord(BaseModel):
    """A human act. Append-only: `seq` chains, nothing is ever updated."""

    model_config = ConfigDict(frozen=True)

    id: str
    run_id: str
    seq: int = Field(ge=1)
    verdict: Verdict
    #: Optional by design — approving without explanation is legitimate (AC-3).
    comment: str | None = None
    principal: Principal
    decided_at: datetime = Field(default_factory=_now)

    def matches(self, verdict: Verdict, comment: str | None, principal: Principal) -> bool:
        """Same actor, same verdict, same comment — a retry, not a new decision.

        Used for idempotency (AC-6): a client that retries a timed-out request
        must not produce a second audit entry.
        """
        return (
            self.verdict == verdict
            and self.comment == comment
            and self.principal.subject == principal.subject
        )
