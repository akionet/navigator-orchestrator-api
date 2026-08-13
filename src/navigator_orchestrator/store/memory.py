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

__all__ = ["InMemoryRunStore"]


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
            updated = run.model_copy(
                update={
                    "state": state,
                    "gate_payload": gate_payload if gate_payload is not None else run.gate_payload,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._runs[run_id] = updated
            return updated

    async def claim_run(self, worker: str, workflows: Sequence[str] = ()) -> RunRecord | None:
        """Atomically take one `queued` run for `worker` (`DESIGN-WRK-001` §3.2).

        Returns `None` when there is nothing to do, which is the ordinary case
        for a polling worker and not an error.

        The whole point is that it is atomic: two workers polling the same store
        must never take the same run. Here that is the existing lock; in
        Postgres it is `SELECT … FOR UPDATE SKIP LOCKED`. Anything weaker
        double-executes side effects the first time two workers are running,
        which is exactly when nobody is watching closely.

        An empty `workflows` claims any queued run. A worker that can only load
        some projects passes the names it can actually execute.
        """
        async with self._lock:
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
            claimed = queued[0].model_copy(
                update={"state": "running", "updated_at": datetime.now(UTC)}
            )
            self._runs[claimed.id] = claimed
            return claimed

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
