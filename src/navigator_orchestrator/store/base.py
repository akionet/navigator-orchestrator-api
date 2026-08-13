"""`RunStore` protocol (SPEC-AIP-003 §3.3).

One interface, two implementations: in-memory for the hermetic suite, Postgres
for deployment. The same test suite runs against both — an audit guarantee that
only holds in one of them is not a guarantee.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from navigator_orchestrator.store.models import (
    DecisionRecord,
    Principal,
    RunRecord,
    RunState,
    Verdict,
)

__all__ = ["RunConflictError", "RunNotFoundError", "RunStore"]


class RunNotFoundError(KeyError):
    """No run under that id. Surfaces as HTTP 404."""

    status_code = 404


class RunConflictError(RuntimeError):
    """The run is not in a state that allows this. Surfaces as HTTP 409.

    Carries the current state so the caller is told *why*, not just refused.
    """

    status_code = 409

    def __init__(self, run_id: str, state: RunState, detail: str) -> None:
        self.run_id = run_id
        self.state = state
        super().__init__(f"run {run_id!r} is {state!r}: {detail}")


class RunStore(Protocol):
    async def create_run(
        self,
        run_id: str,
        workflow: str,
        policy: dict[str, Any],
        created_by: Principal | None = None,
    ) -> RunRecord: ...

    async def get_run(self, run_id: str) -> RunRecord: ...

    async def list_runs(
        self,
        workflow: str | None = None,
        state: RunState | None = None,
        limit: int = 50,
    ) -> list[RunRecord]: ...

    async def mark_state(
        self,
        run_id: str,
        state: RunState,
        gate_payload: dict[str, Any] | None = None,
    ) -> RunRecord: ...

    async def append_decision(
        self,
        run_id: str,
        verdict: Verdict,
        principal: Principal,
        comment: str | None = None,
    ) -> tuple[DecisionRecord, bool]:
        """Append a decision. Returns `(record, is_new)`.

        `is_new=False` means an identical decision already existed — a retry,
        not a second act (AC-6). Callers use it to avoid re-entering the graph.
        """
        ...

    async def decisions_for(self, run_id: str) -> list[DecisionRecord]: ...

    async def claim_run(self, worker: str, workflows: Sequence[str] = ()) -> RunRecord | None:
        """Atomically take one `queued` run (`DESIGN-WRK-001` §3.2).

        The one operation a queue needs that a run store does not: two workers
        polling the same store must never claim the same run. `None` means
        nothing is waiting, which is the ordinary case rather than an error.

        An empty `workflows` claims anything queued; a worker that can only load
        some projects passes the names it can actually execute.
        """
        ...

    async def expire_runs(self, older_than_days: int) -> int:
        """Move abandoned `awaiting_decision` runs to `cancelled` (§5.3).

        Decision chains are never touched — an abandoned run is not a deleted
        audit record.
        """
        ...
