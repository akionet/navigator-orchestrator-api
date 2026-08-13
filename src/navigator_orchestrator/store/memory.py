"""In-process `RunStore` (SPEC-AIP-003 §3.3).

Backs the hermetic suite and single-node dev. The locking is not decoration:
it gives the same one-winner semantics as the Postgres row lock, so the
concurrency tests mean the same thing against either implementation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from navigator_orchestrator.store.base import RunConflictError, RunNotFoundError
from navigator_orchestrator.store.models import (
    DecisionRecord,
    Principal,
    RunRecord,
    RunState,
    Verdict,
)

__all__ = ["DEFAULT_LEASE_SECONDS", "InMemoryRunStore"]

#: How long a claim holds a run before the queue takes it back. Generous, because
#: reclaiming a run that is merely slow re-executes its side effects — the
#: failure this is meant to prevent, arrived at from the other direction. A
#: worker with genuinely long steps renews rather than asking for a longer lease.
DEFAULT_LEASE_SECONDS = 300.0


@dataclass
class InMemoryRunStore:
    _runs: dict[str, RunRecord] = field(default_factory=dict)
    _decisions: dict[str, list[DecisionRecord]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def create_run(
        self,
        run_id: str,
        workflow: str,
        policy: dict[str, Any],
        created_by: Principal | None = None,
    ) -> RunRecord:
        async with self._lock:
            if run_id in self._runs:
                raise RunConflictError(run_id, self._runs[run_id].state, "already exists")
            record = RunRecord(id=run_id, workflow=workflow, policy=policy, created_by=created_by)
            self._runs[run_id] = record
            return record

    async def get_run(self, run_id: str) -> RunRecord:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"unknown run {run_id!r}") from exc

    async def list_runs(
        self,
        workflow: str | None = None,
        state: RunState | None = None,
        limit: int = 50,
    ) -> list[RunRecord]:
        matches = [
            run
            for run in self._runs.values()
            if (workflow is None or run.workflow == workflow)
            and (state is None or run.state == state)
        ]
        # Stable newest-first ordering: ids break timestamp ties identically in
        # memory and in a future Postgres `ORDER BY updated_at DESC, id DESC`.
        matches.sort(key=lambda r: (r.updated_at, r.id), reverse=True)
        return matches[:limit]

    async def mark_state(
        self,
        run_id: str,
        state: RunState,
        gate_payload: dict[str, Any] | None = None,
    ) -> RunRecord:
        async with self._lock:
            run = await self.get_run(run_id)
            # Leaving `running` releases the lease. A run paused at a gate is not
            # being worked on, and a lease outliving the work it covers would let
            # the reclaimer queue a run somebody is waiting on a human for.
            holds_lease = state == "running"
            updated = run.model_copy(
                update={
                    "state": state,
                    "gate_payload": gate_payload if gate_payload is not None else run.gate_payload,
                    "leased_by": run.leased_by if holds_lease else None,
                    "lease_expires_at": run.lease_expires_at if holds_lease else None,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._runs[run_id] = updated
            return updated

    async def claim_run(
        self,
        worker: str,
        workflows: Sequence[str] = (),
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> RunRecord | None:
        """Atomically lease one `queued` run to `worker` (`DESIGN-WRK-001` §3.2).

        Returns `None` when there is nothing to do, which is the ordinary case
        for a polling worker and not an error.

        The whole point is that it is atomic: two workers polling the same store
        must never take the same run. Here that is the existing lock; in
        Postgres it is `SELECT … FOR UPDATE SKIP LOCKED`. Anything weaker
        double-executes side effects the first time two workers are running,
        which is exactly when nobody is watching closely.

        **Expired leases are reclaimed first**, so the queue heals itself
        without a scheduler and without the worker being asked to notice its own
        death. A process killed mid-run cannot run cleanup — that is what being
        killed means — so recovery cannot be its responsibility.

        An empty `workflows` claims any queued run. A worker that can only load
        some projects passes the names it can actually execute.
        """
        async with self._lock:
            self._reclaim_locked()
            queued = [
                run
                for run in self._runs.values()
                if run.state == "queued" and (not workflows or run.workflow in workflows)
            ]
            if not queued:
                return None
            # Oldest first: a queue that serves newest-first starves the runs
            # that have already waited longest.
            queued.sort(key=lambda r: (r.created_at, r.id))
            now = datetime.now(UTC)
            claimed = queued[0].model_copy(
                update={
                    "state": "running",
                    "leased_by": worker,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            self._runs[claimed.id] = claimed
            return claimed

    async def renew_lease(
        self, run_id: str, worker: str, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> RunRecord:
        """Extend the lease on a run this worker still holds.

        For work that legitimately outlives one lease. Refuses if the lease has
        already been reclaimed and given to someone else — two workers believing
        they hold the same run is the thing leases exist to prevent, and a
        renewal that resurrects a lost claim would reintroduce it.
        """
        async with self._lock:
            run = await self.get_run(run_id)
            if run.leased_by != worker:
                raise RunConflictError(
                    run_id, run.state, f"leased by {run.leased_by!r}, not {worker!r}"
                )
            now = datetime.now(UTC)
            renewed = run.model_copy(
                update={
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
            self._runs[run_id] = renewed
            return renewed

    async def reclaim_expired_leases(self) -> int:
        """Return runs whose lease lapsed to `queued`. Engine-side recovery.

        Called on every claim, so it needs no scheduler. Exposed separately so
        an operator can ask how many runs a crash stranded.
        """
        async with self._lock:
            return self._reclaim_locked()

    def _reclaim_locked(self) -> int:
        """Caller holds `self._lock`."""
        now = datetime.now(UTC)
        stranded = [
            run
            for run in self._runs.values()
            if run.state == "running"
            and run.lease_expires_at is not None
            and run.lease_expires_at <= now
        ]
        for run in stranded:
            self._runs[run.id] = run.model_copy(
                update={
                    "state": "queued",
                    "leased_by": None,
                    "lease_expires_at": None,
                    "updated_at": now,
                }
            )
        return len(stranded)

    async def append_decision(
        self,
        run_id: str,
        verdict: Verdict,
        principal: Principal,
        comment: str | None = None,
    ) -> tuple[DecisionRecord, bool]:
        async with self._lock:
            run = await self.get_run(run_id)
            existing = self._decisions.get(run_id, [])

            if run.state != "awaiting_decision":
                # A retry of the decision that moved it is fine; a different
                # verdict on a decided run is not (AC-6).
                if existing and existing[-1].matches(verdict, comment, principal):
                    return existing[-1], False
                raise RunConflictError(run_id, run.state, "not awaiting a decision")

            record = DecisionRecord(
                id=uuid4().hex,
                run_id=run_id,
                seq=len(existing) + 1,
                verdict=verdict,
                comment=comment,
                principal=principal,
            )
            self._decisions.setdefault(run_id, []).append(record)
            return record, True

    async def decisions_for(self, run_id: str) -> list[DecisionRecord]:
        await self.get_run(run_id)
        return list(self._decisions.get(run_id, []))

    async def expire_runs(self, older_than_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        async with self._lock:
            stale = [
                run
                for run in self._runs.values()
                if run.state == "awaiting_decision" and run.updated_at < cutoff
            ]
            for run in stale:
                self._runs[run.id] = run.model_copy(
                    update={"state": "cancelled", "updated_at": datetime.now(UTC)}
                )
            return len(stale)

    def clear(self) -> None:
        self._runs.clear()
        self._decisions.clear()
