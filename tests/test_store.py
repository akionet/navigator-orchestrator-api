"""Run + decision records (SPEC-AIP-003 §3.3, TODO-1).

These run against `InMemoryRunStore`; S8 re-runs the same suite against
Postgres. An audit guarantee that only holds in one implementation is not a
guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from navigator_orchestrator.store import (
    InMemoryRunStore,
    Principal,
    RunConflictError,
    RunNotFoundError,
)

PRIYA = Principal(subject="priya", issuer="header")
SAM = Principal(subject="sam", issuer="header")


@pytest.fixture
def store() -> InMemoryRunStore:
    return InMemoryRunStore()


async def _paused(store: InMemoryRunStore, run_id: str = "r1") -> None:
    await store.create_run(run_id, "approval", {"model": "fake:echo"})
    await store.mark_state(run_id, "awaiting_decision", {"proposal": "ship it"})


async def test_create_and_get(store: InMemoryRunStore) -> None:
    run = await store.create_run("r1", "approval", {"model": "fake:echo"}, PRIYA)
    assert run.state == "running"
    assert run.created_by == PRIYA
    assert (await store.get_run("r1")).id == "r1"


async def test_unknown_run_is_404(store: InMemoryRunStore) -> None:
    with pytest.raises(RunNotFoundError):
        await store.get_run("nope")
    assert RunNotFoundError.status_code == 404


async def test_duplicate_run_id_is_a_conflict(store: InMemoryRunStore) -> None:
    await store.create_run("r1", "approval", {})
    with pytest.raises(RunConflictError):
        await store.create_run("r1", "approval", {})


async def test_gate_payload_is_kept_opaque(store: InMemoryRunStore) -> None:
    """The engine stores it and never interprets it."""
    await _paused(store)
    run = await store.get_run("r1")
    assert run.state == "awaiting_decision"
    assert run.gate_payload == {"proposal": "ship it"}


async def test_marking_state_preserves_the_gate_payload(store: InMemoryRunStore) -> None:
    await _paused(store)
    run = await store.mark_state("r1", "completed")
    assert run.gate_payload == {"proposal": "ship it"}


async def test_listing_filters_by_workflow_and_state(store: InMemoryRunStore) -> None:
    await _paused(store, "r1")
    await store.create_run("r2", "approval", {})
    await store.create_run("r3", "echo", {})

    awaiting = await store.list_runs(state="awaiting_decision")
    assert [r.id for r in awaiting] == ["r1"]
    assert {r.id for r in await store.list_runs(workflow="approval")} == {"r1", "r2"}
    assert len(await store.list_runs(limit=2)) == 2


async def test_listing_uses_id_as_a_stable_timestamp_tie_breaker(
    store: InMemoryRunStore,
) -> None:
    tied = datetime(2026, 8, 13, tzinfo=UTC)
    for run_id in ("r-a", "r-c", "r-b"):
        await store.create_run(run_id, "echo", {})
        store._runs[run_id] = store._runs[run_id].model_copy(update={"updated_at": tied})

    assert [run.id for run in await store.list_runs()] == ["r-c", "r-b", "r-a"]


async def test_decisions_are_appended_not_updated(store: InMemoryRunStore) -> None:
    await _paused(store)
    first, is_new = await store.append_decision("r1", "amend", PRIYA, "too salty")
    assert (first.seq, is_new) == (1, True)

    # `amend` opens another round: the run pauses again.
    await store.mark_state("r1", "awaiting_decision")
    second, is_new = await store.append_decision("r1", "approve", SAM)
    assert (second.seq, is_new) == (2, True)

    chain = await store.decisions_for("r1")
    assert [d.verdict for d in chain] == ["amend", "approve"]
    assert [d.seq for d in chain] == [1, 2]


async def test_comment_is_optional(store: InMemoryRunStore) -> None:
    """Approving without explanation is legitimate (AC-3)."""
    await _paused(store)
    record, _ = await store.append_decision("r1", "approve", PRIYA)
    assert record.comment is None


async def test_every_decision_carries_an_actor(store: InMemoryRunStore) -> None:
    await _paused(store)
    record, _ = await store.append_decision("r1", "approve", PRIYA, "fine")
    assert record.principal.subject == "priya"
    assert record.decided_at.tzinfo is not None


async def test_identical_repeat_is_idempotent(store: InMemoryRunStore) -> None:
    """A retried request must not produce a second audit entry (AC-6)."""
    await _paused(store)
    first, new_first = await store.append_decision("r1", "approve", PRIYA, "fine")
    await store.mark_state("r1", "completed")

    again, new_again = await store.append_decision("r1", "approve", PRIYA, "fine")
    assert new_first is True
    assert new_again is False
    assert again.id == first.id
    assert len(await store.decisions_for("r1")) == 1


async def test_conflicting_decision_on_a_decided_run_is_refused(
    store: InMemoryRunStore,
) -> None:
    await _paused(store)
    await store.append_decision("r1", "approve", PRIYA, "fine")
    await store.mark_state("r1", "completed")

    with pytest.raises(RunConflictError) as excinfo:
        await store.append_decision("r1", "reject", SAM, "no")
    assert excinfo.value.state == "completed"
    assert excinfo.value.status_code == 409
    assert len(await store.decisions_for("r1")) == 1


async def test_deciding_a_running_run_is_refused(store: InMemoryRunStore) -> None:
    await store.create_run("r1", "approval", {})
    with pytest.raises(RunConflictError):
        await store.append_decision("r1", "approve", PRIYA)


async def test_expiry_cancels_abandoned_runs_but_keeps_the_chain(
    store: InMemoryRunStore,
) -> None:
    """An abandoned run is not a deleted audit record (§5.3)."""
    await _paused(store)
    await store.append_decision("r1", "amend", PRIYA, "revise")
    await store.mark_state("r1", "awaiting_decision")

    store._runs["r1"] = store._runs["r1"].model_copy(
        update={"updated_at": datetime.now(UTC) - timedelta(days=6)}
    )
    assert await store.expire_runs(5) == 1
    assert (await store.get_run("r1")).state == "cancelled"
    assert len(await store.decisions_for("r1")) == 1


async def test_expiry_leaves_fresh_runs_alone(store: InMemoryRunStore) -> None:
    await _paused(store)
    assert await store.expire_runs(5) == 0
    assert (await store.get_run("r1")).state == "awaiting_decision"
