"""Checkpointer seam (SPEC-AIP-002 §3.6, TODO-4).

`echo` runs checkpointer-off — the house rule is not to over-checkpoint
short-lived graphs. These tests prove the wiring works so ATT's HITL graphs
(R1) inherit a proven seam.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from navigator_orchestrator.engine.checkpoint import checkpointer_scope, make_memory_checkpointer
from navigator_orchestrator.workflows.echo import EchoWorkflow


def test_echo_is_checkpointer_off_by_default() -> None:
    assert EchoWorkflow().checkpointed is False


async def test_graph_with_a_checkpointer_is_resumable(context: Any) -> None:
    """Smoke: state persists under a thread id and can be read back."""
    saver = make_memory_checkpointer()
    workflow = EchoWorkflow(checkpointer=saver)
    assert workflow.checkpointed is True

    graph = workflow.build_graph(context.runner.deps)
    config = {"configurable": {"thread_id": "t-1"}}
    result = await graph.ainvoke(workflow.initial_state(workflow.Input(text="ping")), config)
    assert result["scratch"]["output"]["text"] == "ping"

    snapshot = await graph.aget_state(config)
    assert snapshot.values["scratch"]["output"]["text"] == "ping"


async def test_none_kind_yields_no_checkpointer() -> None:
    async with checkpointer_scope("none") as saver:
        assert saver is None


async def test_memory_kind_yields_a_saver() -> None:
    async with checkpointer_scope("memory") as saver:
        assert saver is not None


async def test_postgres_kind_requires_a_dsn() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        async with checkpointer_scope("postgres", None):
            pass  # pragma: no cover


@pytest.mark.skipif(
    not os.getenv("NAVIGATOR_DATABASE_URL"),
    reason="no Postgres reachable; CI runs this against a service container",
)
async def test_postgres_checkpointer_connects() -> None:  # pragma: no cover - CI only
    async with checkpointer_scope("postgres", os.environ["NAVIGATOR_DATABASE_URL"]) as saver:
        assert saver is not None
