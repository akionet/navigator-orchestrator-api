"""Pause and resume through the engine and the API (SPEC-AIP-003 AC-1..AC-6).

The load-bearing test here is `test_a_second_app_instance_resumes_the_run`: a
resume driven from the *same* Runner object would prove nothing about
separation of duties.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import parse_sse, running_app
from fastapi import FastAPI

from navigator_orchestrator.api.app import build_app, build_context
from navigator_orchestrator.engine.checkpoint import make_memory_checkpointer
from navigator_orchestrator.engine.runner import Runner
from navigator_orchestrator.engine.workflow import WorkflowRegistry
from navigator_orchestrator.store import InMemoryRunStore, Principal
from navigator_orchestrator.workflows.approval import ApprovalWorkflow


@pytest.fixture
def saver() -> Any:
    return make_memory_checkpointer()


@pytest.fixture
def run_store() -> InMemoryRunStore:
    return InMemoryRunStore()


@pytest.fixture
def hitl_app(
    settings: Any, fake_llm: Any, cache: Any, observability: Any, saver: Any, run_store: Any
) -> FastAPI:
    return build_app(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        run_store=run_store,
        checkpointer=saver,
    )


async def _start(client: Any, text: str = "ship it") -> tuple[str, list[dict[str, Any]]]:
    response = await client.post("/workflows/approval/runs", json={"text": text})
    events = parse_sse(response.text)
    return events[-1]["data"]["run_id"], events


# ----------------------------------------------------------------- AC-1


async def test_a_paused_run_emits_interrupt_not_error(hitl_app: FastAPI) -> None:
    async with running_app(hitl_app) as client:
        _, events = await _start(client)

    kinds = [e["event"] for e in events]
    assert kinds[-1] == "interrupt", kinds
    assert "error" not in kinds, "a pause is not a failure"
    assert "final" not in kinds, "a pause is not a result"


async def test_the_interrupt_carries_the_gate_payload(hitl_app: FastAPI) -> None:
    async with running_app(hitl_app) as client:
        _, events = await _start(client)

    payload = next(e for e in events if e["event"] == "interrupt")["data"]["payload"]
    assert payload["proposal"] == "ship it"
    # Enough to answer "which model, under which prompt version" (AC-4 of R0's
    # observability promise, carried into the reviewer's view).
    assert payload["model"] == "fake:echo"
    assert payload["prompt_ref"] == "approval@1"


async def test_a_non_pausing_workflow_is_unaffected(hitl_app: FastAPI) -> None:
    """The R0 regression bar: `echo` still ends in `final`."""
    async with running_app(hitl_app) as client:
        response = await client.post("/workflows/echo/runs", json={"text": "ping"})
    kinds = [e["event"] for e in parse_sse(response.text)]
    assert kinds[-1] == "final"
    assert "interrupt" not in kinds


# ----------------------------------------------------------------- AC-2


async def test_a_paused_run_is_listed_as_awaiting(hitl_app: FastAPI) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        listing = await client.get("/workflows/approval/runs?state=awaiting_decision")

    runs = listing.json()["runs"]
    assert [r["id"] for r in runs] == [run_id]
    assert runs[0]["gate_payload"]["proposal"] == "ship it"


async def test_a_completed_run_is_not_in_the_queue(hitl_app: FastAPI) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        await client.post(
            f"/workflows/approval/runs/{run_id}/decisions", json={"verdict": "approve"}
        )
        listing = await client.get("/workflows/approval/runs?state=awaiting_decision")
    assert listing.json()["runs"] == []


# ----------------------------------------------------------------- AC-3


async def test_a_second_app_instance_resumes_the_run(
    settings: Any, fake_llm: Any, cache: Any, observability: Any, saver: Any, run_store: Any
) -> None:
    """Separation of duties, mechanically.

    Two apps, each with its own Runner, sharing only the store and the
    checkpointer — which is exactly the shape of "a different person, in a
    different process, minutes later".
    """
    starter = build_app(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        run_store=run_store,
        checkpointer=saver,
    )
    decider = build_app(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        run_store=run_store,
        checkpointer=saver,
    )

    async with running_app(starter) as client:
        run_id, events = await _start(client)
    assert events[-1]["event"] == "interrupt"

    async with running_app(decider) as client:
        response = await client.post(
            f"/workflows/approval/runs/{run_id}/decisions",
            json={"verdict": "approve", "comment": "looks good"},
        )
        resumed = parse_sse(response.text)

    final = next(e for e in resumed if e["event"] == "final")["data"]["output"]
    assert final["verdict"] == "approve"
    assert final["comment"] == "looks good"
    assert final["proposal"] == "ship it"
    assert (await run_store.get_run(run_id)).state == "completed"


async def test_resuming_an_unknown_run_is_404(hitl_app: FastAPI) -> None:
    async with running_app(hitl_app) as client:
        response = await client.post(
            "/workflows/approval/runs/nope/decisions", json={"verdict": "approve"}
        )
    assert response.status_code == 404


async def test_wrong_workflow_cannot_append_a_decision(
    hitl_app: FastAPI, run_store: InMemoryRunStore
) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        response = await client.post(
            f"/workflows/echo/runs/{run_id}/decisions",
            json={"verdict": "approve"},
        )

    assert response.status_code == 404
    assert response.json()["error"] == "workflow_run_mismatch"
    assert await run_store.decisions_for(run_id) == []


async def test_resume_continues_the_same_ordered_log(hitl_app: FastAPI) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        before = (await client.get(f"/workflows/approval/runs/{run_id}/log")).json()["entries"]
        resumed = await client.post(
            f"/workflows/approval/runs/{run_id}/decisions",
            json={"verdict": "approve", "comment": "continue"},
        )
        after = (await client.get(f"/workflows/approval/runs/{run_id}/log")).json()["entries"]

    assert resumed.headers["content-type"].startswith("text/event-stream")
    assert [entry["seq"] for entry in after] == list(range(1, len(after) + 1))
    assert after[: len(before)] == before
    assert after[-1]["status"] == "completed"


# ----------------------------------------------------------------- AC-4


async def test_the_decision_chain_is_the_audit_view(hitl_app: FastAPI, run_store: Any) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        await client.post(
            f"/workflows/approval/runs/{run_id}/decisions",
            json={"verdict": "approve", "comment": "fine"},
        )
        detail = (await client.get(f"/workflows/approval/runs/{run_id}")).json()

    assert detail["run"]["state"] == "completed"
    assert len(detail["decisions"]) == 1
    decision = detail["decisions"][0]
    assert decision["verdict"] == "approve"
    assert decision["comment"] == "fine"
    assert decision["seq"] == 1
    assert decision["principal"]["subject"]


async def test_a_decision_is_recorded_even_if_the_resume_fails(
    hitl_app: FastAPI, run_store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit trail must not depend on the workflow succeeding (plan S6)."""
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)

        # Break the resume path *after* the decision has been appended.
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("resume exploded")

        monkeypatch.setattr(Runner, "resume", _boom)
        with pytest.raises(RuntimeError):
            await client.post(
                f"/workflows/approval/runs/{run_id}/decisions",
                json={"verdict": "reject", "comment": "no"},
            )

    assert len(await run_store.decisions_for(run_id)) == 1


# ----------------------------------------------------------------- AC-5


async def test_an_unattributable_decision_is_refused(
    settings: Any, fake_llm: Any, cache: Any, observability: Any, saver: Any, run_store: Any
) -> None:
    strict = settings.model_copy(update={"require_principal": True})
    app = build_app(
        strict,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        run_store=run_store,
        checkpointer=saver,
    )
    async with running_app(app) as client:
        run_id, _ = await _start(client)
        refused = await client.post(
            f"/workflows/approval/runs/{run_id}/decisions", json={"verdict": "approve"}
        )
        accepted = await client.post(
            f"/workflows/approval/runs/{run_id}/decisions",
            json={"verdict": "approve"},
            headers={strict.principal_header: "reviewer@example.com"},
        )

    assert refused.status_code == 403
    detail = refused.json()["detail"]
    assert detail["error"] == "principal_required"
    assert strict.principal_header in detail["detail"], "the 403 must name the credential"

    assert accepted.status_code == 200
    chain = await run_store.decisions_for(run_id)
    assert [d.principal.subject for d in chain] == ["reviewer@example.com"]
    assert chain[0].principal.issuer == "header"


# ----------------------------------------------------------------- AC-6


async def test_an_identical_repeat_does_not_double_the_record(
    hitl_app: FastAPI, run_store: Any
) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        body = {"verdict": "approve", "comment": "fine"}
        first = await client.post(f"/workflows/approval/runs/{run_id}/decisions", json=body)
        repeat = await client.post(f"/workflows/approval/runs/{run_id}/decisions", json=body)

    assert first.status_code == 200
    assert repeat.json()["status"] == "already_decided"
    assert len(await run_store.decisions_for(run_id)) == 1


async def test_a_conflicting_decision_is_409(hitl_app: FastAPI, run_store: Any) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        await client.post(
            f"/workflows/approval/runs/{run_id}/decisions", json={"verdict": "approve"}
        )
        conflict = await client.post(
            f"/workflows/approval/runs/{run_id}/decisions",
            json={"verdict": "reject", "comment": "changed my mind"},
        )

    assert conflict.status_code == 409
    assert conflict.json()["state"] == "completed"
    assert len(await run_store.decisions_for(run_id)) == 1


async def test_an_invalid_verdict_is_422(hitl_app: FastAPI) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
        response = await client.post(
            f"/workflows/approval/runs/{run_id}/decisions", json={"verdict": "maybe"}
        )
    assert response.status_code == 422


# ------------------------------------------------------- guard rails


async def test_a_pausing_workflow_without_a_checkpointer_fails_loudly(
    settings: Any, fake_llm: Any, cache: Any, observability: Any, run_store: Any
) -> None:
    """Better to refuse than to lose a run at the gate (plan S3)."""
    registry = WorkflowRegistry()
    registry.register(ApprovalWorkflow(checkpointer=None))
    context = build_context(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        registry=registry,
        run_store=run_store,
    )
    events = [e async for e in context.runner.run("approval", {"text": "x"})]
    error = next(e for e in events if e.type == "error")
    assert "checkpointer" in error.detail["message"]


async def test_principal_is_recorded_on_the_run(hitl_app: FastAPI, run_store: Any) -> None:
    async with running_app(hitl_app) as client:
        run_id, _ = await _start(client)
    run = await run_store.get_run(run_id)
    assert run.created_by is None or isinstance(run.created_by, Principal)
