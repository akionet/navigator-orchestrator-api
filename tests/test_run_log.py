from __future__ import annotations

import asyncio

import pytest

from navigator_orchestrator.store import InMemoryRunLogStore


@pytest.mark.asyncio
async def test_run_log_assigns_monotonic_sequences_and_isolates_runs() -> None:
    store = InMemoryRunLogStore()
    await asyncio.gather(
        *(
            store.append(run_id="r1", workflow="echo", step=f"n{i}", status="started")
            for i in range(20)
        )
    )
    await store.append(run_id="r2", workflow="echo", status="completed")

    entries = await store.read("r1")
    assert [entry.seq for entry in entries] == list(range(1, 21))
    assert all(entry.run_id == "r1" for entry in entries)
    assert [entry.seq for entry in await store.read("r2")] == [1]


@pytest.mark.asyncio
async def test_run_log_returns_copies_of_entry_lists() -> None:
    store = InMemoryRunLogStore()
    await store.append(run_id="r1", workflow="echo", status="completed")
    first = await store.read("r1")
    first.clear()
    assert len(await store.read("r1")) == 1
