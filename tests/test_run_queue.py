"""Claiming queued runs (`DESIGN-WRK-001` §3.2).

The first piece of the worker: the API creates a run it is not allowed to
execute, and a separate process takes it. Everything else in that design
depends on the claim being atomic, so it is tested before anything is built on
top of it.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from navigator_orchestrator.store import InMemoryRunStore, Principal


def _actor() -> Principal:
    return Principal(subject="api", issuer="test")


async def _new(store: InMemoryRunStore, workflow: str) -> str:
    run = await store.create_run(
        run_id=uuid4().hex, workflow=workflow, policy={}, created_by=_actor()
    )
    return run.id


async def _queue(store: InMemoryRunStore, workflow: str, count: int = 1) -> list[str]:
    ids = []
    for _ in range(count):
        run_id = await _new(store, workflow)
        await store.mark_state(run_id, "queued")
        ids.append(run_id)
    return ids


@pytest.mark.asyncio
async def test_an_empty_queue_returns_none_rather_than_raising() -> None:
    """The ordinary case for a polling worker, not an error."""
    store = InMemoryRunStore()
    assert await store.claim_run("worker-1") is None


@pytest.mark.asyncio
async def test_claiming_moves_a_run_to_running() -> None:
    store = InMemoryRunStore()
    (run_id,) = await _queue(store, "sample")

    claimed = await store.claim_run("worker-1")

    assert claimed is not None
    assert claimed.id == run_id
    assert claimed.state == "running", "a claimed run must not stay claimable"
    assert (await store.get_run(run_id)).state == "running"


@pytest.mark.asyncio
async def test_a_claimed_run_is_not_claimed_twice() -> None:
    store = InMemoryRunStore()
    await _queue(store, "sample")

    first = await store.claim_run("worker-1")
    second = await store.claim_run("worker-2")

    assert first is not None
    assert second is None, "the second worker must find nothing, not the same run"


@pytest.mark.asyncio
async def test_concurrent_workers_never_share_a_run() -> None:
    """The property the whole design rests on.

    Anything weaker double-executes side effects the first time two workers run
    at once — which is exactly when nobody is watching closely.
    """
    store = InMemoryRunStore()
    queued = await _queue(store, "sample", count=20)

    claims = await asyncio.gather(*(store.claim_run(f"worker-{i}") for i in range(40)))
    taken = [claim.id for claim in claims if claim is not None]

    assert len(taken) == len(queued), "every queued run should be claimed exactly once"
    assert len(set(taken)) == len(taken), "no run may be claimed twice"


@pytest.mark.asyncio
async def test_the_oldest_queued_run_is_claimed_first() -> None:
    """A queue serving newest-first starves whatever has waited longest."""
    store = InMemoryRunStore()
    first, second = await _queue(store, "sample", count=2)

    assert (claimed := await store.claim_run("worker-1")) is not None
    assert claimed.id == first
    assert (claimed := await store.claim_run("worker-1")) is not None
    assert claimed.id == second


@pytest.mark.asyncio
async def test_a_worker_claims_only_workflows_it_can_run() -> None:
    """A worker that cannot load a project must not take its runs hostage."""
    store = InMemoryRunStore()
    await _queue(store, "alpha")
    (beta_id,) = await _queue(store, "beta")

    claimed = await store.claim_run("worker-1", workflows=("beta",))

    assert claimed is not None
    assert claimed.id == beta_id
    assert claimed.workflow == "beta"


@pytest.mark.asyncio
async def test_only_queued_runs_are_claimable() -> None:
    """A run waiting on a human is not work; neither is a finished one."""
    store = InMemoryRunStore()
    running = await _new(store, "sample")
    paused = await _new(store, "sample")
    await store.mark_state(paused, "awaiting_decision", {"step": "review"})
    done = await _new(store, "sample")
    await store.mark_state(done, "completed")

    assert (await store.get_run(running)).state == "running"
    assert await store.claim_run("worker-1") is None
