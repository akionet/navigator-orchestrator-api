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

from navigator_orchestrator.store import InMemoryRunStore, Principal, RunConflictError


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


# ── leases: recovery is the store's job, not the worker's ────────────────────


@pytest.mark.asyncio
async def test_a_claim_carries_a_lease() -> None:
    store = InMemoryRunStore()
    await _queue(store, "sample")

    claimed = await store.claim_run("worker-1", lease_seconds=60)

    assert claimed is not None
    assert claimed.leased_by == "worker-1"
    assert claimed.lease_expires_at is not None


@pytest.mark.asyncio
async def test_a_dead_worker_does_not_strand_a_run_forever() -> None:
    """The failure this exists for.

    A worker killed mid-run cannot run its own cleanup — that is what being
    killed means — so recovery cannot be its responsibility. A lapsed lease
    returns the run to the queue with no scheduler and no cooperation from the
    process that died.
    """
    store = InMemoryRunStore()
    (run_id,) = await _queue(store, "sample")
    await store.claim_run("doomed-worker", lease_seconds=-1)  # already expired

    assert (await store.get_run(run_id)).state == "running"

    recovered = await store.claim_run("healthy-worker")

    assert recovered is not None
    assert recovered.id == run_id
    assert recovered.leased_by == "healthy-worker"


@pytest.mark.asyncio
async def test_a_live_lease_is_not_stolen() -> None:
    """Reclaiming a run that is merely slow re-executes its side effects."""
    store = InMemoryRunStore()
    await _queue(store, "sample")
    await store.claim_run("worker-1", lease_seconds=300)

    assert await store.claim_run("worker-2") is None
    assert await store.reclaim_expired_leases() == 0


@pytest.mark.asyncio
async def test_renewing_extends_work_that_outlives_one_lease() -> None:
    store = InMemoryRunStore()
    (run_id,) = await _queue(store, "sample")
    claimed = await store.claim_run("worker-1", lease_seconds=1)
    assert claimed is not None

    renewed = await store.renew_lease(run_id, "worker-1", lease_seconds=600)

    assert renewed.lease_expires_at is not None
    assert claimed.lease_expires_at is not None
    assert renewed.lease_expires_at > claimed.lease_expires_at


@pytest.mark.asyncio
async def test_a_lost_claim_cannot_be_renewed_back() -> None:
    """Two workers believing they hold one run is what leases prevent."""
    store = InMemoryRunStore()
    (run_id,) = await _queue(store, "sample")
    await store.claim_run("slow-worker", lease_seconds=-1)
    await store.claim_run("healthy-worker")

    with pytest.raises(RunConflictError, match="healthy-worker"):
        await store.renew_lease(run_id, "slow-worker")


@pytest.mark.asyncio
async def test_leaving_running_releases_the_lease() -> None:
    """A run paused at a gate is not being worked on.

    A lease outliving the work it covers would let the reclaimer queue a run
    that is waiting on a human.
    """
    store = InMemoryRunStore()
    (run_id,) = await _queue(store, "sample")
    await store.claim_run("worker-1", lease_seconds=-1)

    paused = await store.mark_state(run_id, "awaiting_decision", {"step": "review"})

    assert paused.leased_by is None
    assert paused.lease_expires_at is None
    assert await store.reclaim_expired_leases() == 0, "a paused run must not be reclaimed"


@pytest.mark.asyncio
async def test_reclaiming_reports_how_many_a_crash_stranded() -> None:
    store = InMemoryRunStore()
    ids = await _queue(store, "sample", count=3)
    # Claim them all live first — claiming with an already-dead lease would let
    # each claim reclaim the previous one, and only ever strand one.
    for _ in ids:
        assert await store.claim_run("doomed", lease_seconds=300) is not None
    for run_id in ids:
        await store.renew_lease(run_id, "doomed", lease_seconds=-1)

    assert await store.reclaim_expired_leases() == 3
    assert await store.reclaim_expired_leases() == 0, "reclaiming twice is not three more"


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
