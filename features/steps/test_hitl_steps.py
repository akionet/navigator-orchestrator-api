"""AC-1..AC-6 — the `hitl-*.feature` files (SPEC-AIP-003)."""

from __future__ import annotations

import json
from typing import Any

from conftest import parse_sse, running_app
from pytest_bdd import given, parsers, scenarios, then, when

from navigator_orchestrator.api.app import build_app
from navigator_orchestrator.engine.checkpoint import make_memory_checkpointer
from navigator_orchestrator.store import InMemoryRunStore

scenarios("hitl-runtime.feature")
scenarios("hitl-runs.feature")
scenarios("hitl-resume.feature")
scenarios("hitl-audit.feature")


def _build(bdd_context: dict[str, Any], settings: Any, fake_llm: Any, cache: Any, obs: Any) -> Any:
    """An app sharing this scenario's store and checkpointer."""
    bdd_context.setdefault("store", InMemoryRunStore())
    bdd_context.setdefault("saver", make_memory_checkpointer())
    return build_app(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=obs,
        run_store=bdd_context["store"],
        checkpointer=bdd_context["saver"],
    )


@given('the "approval" workflow is registered with a checkpointer')
def approval_registered(
    bdd_context: dict[str, Any], settings: Any, fake_llm: Any, cache: Any, observability: Any
) -> None:
    bdd_context["app"] = _build(bdd_context, settings, fake_llm, cache, observability)


@given("a deployment that requires an attributable principal")
def strict_deployment(bdd_context: dict[str, Any]) -> None:
    bdd_context["strict"] = True


@given('a run of "approval" paused awaiting a decision')
def paused_run(
    bdd_context: dict[str, Any],
    settings: Any,
    fake_llm: Any,
    cache: Any,
    observability: Any,
    run_async: Any,
) -> None:
    resolved = settings.model_copy(update={"require_principal": bdd_context.get("strict", False)})
    bdd_context["settings"] = resolved
    bdd_context["app"] = _build(bdd_context, resolved, fake_llm, cache, observability)

    async def _start() -> None:
        async with running_app(bdd_context["app"]) as client:
            response = await client.post("/workflows/approval/runs", json={"text": "ship it"})
            events = parse_sse(response.text)
            bdd_context["events"] = events
            bdd_context["run_id"] = events[-1]["data"]["run_id"]

    run_async(_start())


@given("a second app instance sharing the same run store")
def second_instance(
    bdd_context: dict[str, Any], fake_llm: Any, cache: Any, observability: Any
) -> None:
    # Same store and checkpointer, a different Runner: the shape of "someone
    # else, later, elsewhere".
    bdd_context["decider"] = _build(
        bdd_context, bdd_context["settings"], fake_llm, cache, observability
    )


@when(parsers.parse('I run "{name}" with input {payload}'))
def run_workflow(bdd_context: dict[str, Any], run_async: Any, name: str, payload: str) -> None:
    async def _call() -> None:
        async with running_app(bdd_context["app"]) as client:
            response = await client.post(f"/workflows/{name}/runs", json=json.loads(payload))
            bdd_context["events"] = parse_sse(response.text)

    run_async(_call())


def _decide(
    bdd_context: dict[str, Any],
    run_async: Any,
    *,
    app_key: str = "app",
    subject: str = "priya",
    verdict: str = "approve",
    comment: str | None = "looks good",
    anonymous: bool = False,
) -> None:
    body: dict[str, Any] = {"verdict": verdict}
    if comment is not None:
        body["comment"] = comment
    headers = (
        {}
        if anonymous
        else {bdd_context["settings"].principal_header: subject}
        if bdd_context.get("strict")
        else {}
    )

    async def _call() -> None:
        async with running_app(bdd_context[app_key]) as client:
            response = await client.post(
                f"/workflows/approval/runs/{bdd_context['run_id']}/decisions",
                json=body,
                headers=headers,
            )
            bdd_context["response"] = response
            bdd_context.setdefault("responses", []).append(response)

    run_async(_call())


@when(parsers.parse('"{subject}" approves the run through the second instance'))
def approve_via_second(bdd_context: dict[str, Any], run_async: Any, subject: str) -> None:
    _decide(bdd_context, run_async, app_key="decider", subject=subject)


@when(parsers.parse('"{subject}" approves the run without a comment'))
def approve_without_comment(bdd_context: dict[str, Any], run_async: Any, subject: str) -> None:
    _decide(bdd_context, run_async, subject=subject, comment=None)


@when(parsers.parse('"{subject}" approves the run again with the same comment'))
def approve_again(bdd_context: dict[str, Any], run_async: Any, subject: str) -> None:
    _decide(bdd_context, run_async, subject=subject)


@when(parsers.parse('"{subject}" approves the run'))
def approve(bdd_context: dict[str, Any], run_async: Any, subject: str) -> None:
    _decide(bdd_context, run_async, subject=subject)


@when(parsers.parse('"{subject}" rejects the same run'))
def reject(bdd_context: dict[str, Any], run_async: Any, subject: str) -> None:
    _decide(bdd_context, run_async, subject=subject, verdict="reject", comment="no")


@when("an anonymous caller approves the run")
def approve_anonymously(bdd_context: dict[str, Any], run_async: Any) -> None:
    _decide(bdd_context, run_async, anonymous=True)


@when('I list "approval" runs awaiting a decision')
def list_awaiting(bdd_context: dict[str, Any], run_async: Any) -> None:
    async def _call() -> None:
        async with running_app(bdd_context["app"]) as client:
            response = await client.get("/workflows/approval/runs?state=awaiting_decision")
            bdd_context["listing"] = response.json()["runs"]

    run_async(_call())


# ------------------------------------------------------------------ then


@then('I receive an "interrupt" event carrying the gate payload')
def got_interrupt(bdd_context: dict[str, Any]) -> None:
    interrupts = [e for e in bdd_context["events"] if e["event"] == "interrupt"]
    assert len(interrupts) == 1
    assert interrupts[0]["data"]["payload"]["proposal"]


@then(parsers.parse('no "{kind}" event is emitted'))
def no_event_of_kind(bdd_context: dict[str, Any], kind: str) -> None:
    assert kind not in [e["event"] for e in bdd_context["events"]]


@then(parsers.parse('the run ends with a "{kind}" event'))
def ends_with(bdd_context: dict[str, Any], kind: str) -> None:
    assert bdd_context["events"][-1]["event"] == kind


@then("the paused run is listed")
def paused_listed(bdd_context: dict[str, Any]) -> None:
    assert [r["id"] for r in bdd_context["listing"]] == [bdd_context["run_id"]]


@then("its gate payload is available to the reviewer")
def gate_payload_available(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["listing"][0]["gate_payload"]["proposal"] == "ship it"


@then("the queue is empty")
def queue_empty(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["listing"] == []


@then("the run completes and emits a final event")
def resumed_to_final(bdd_context: dict[str, Any]) -> None:
    events = parse_sse(bdd_context["response"].text)
    final = next(e for e in events if e["event"] == "final")
    assert final["data"]["output"]["verdict"] == "approve"


@then(parsers.parse('the run state is "{state}"'))
def run_state_is(bdd_context: dict[str, Any], run_async: Any, state: str) -> None:
    record = run_async(bdd_context["store"].get_run(bdd_context["run_id"]))
    assert record.state == state


@then(parsers.parse("the response is a {status:d} naming the current state"))
def conflict_response(bdd_context: dict[str, Any], status: int) -> None:
    response = bdd_context["response"]
    assert response.status_code == status
    assert response.json()["state"] == "completed"


@then("the second response reports the run as already decided")
def already_decided(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["responses"][-1].json()["status"] == "already_decided"


@then("the decision chain still has exactly one entry")
def chain_has_one_entry(bdd_context: dict[str, Any], run_async: Any) -> None:
    chain = run_async(bdd_context["store"].decisions_for(bdd_context["run_id"]))
    assert len(chain) == 1, [d.model_dump() for d in chain]


@then(parsers.parse('the decision chain records "{subject}" with verdict "{verdict}"'))
def chain_records(bdd_context: dict[str, Any], run_async: Any, subject: str, verdict: str) -> None:
    chain = run_async(bdd_context["store"].decisions_for(bdd_context["run_id"]))
    assert chain[-1].verdict == verdict
    # Without `require_principal` the deployment cannot attribute the act; the
    # AC-5 scenario is the one that proves attribution.
    if bdd_context.get("strict"):
        assert chain[-1].principal.subject == subject


@then("the decision carries a timestamp")
def chain_timestamped(bdd_context: dict[str, Any], run_async: Any) -> None:
    chain = run_async(bdd_context["store"].decisions_for(bdd_context["run_id"]))
    assert chain[-1].decided_at.tzinfo is not None


@then("the recorded comment is empty")
def comment_empty(bdd_context: dict[str, Any], run_async: Any) -> None:
    chain = run_async(bdd_context["store"].decisions_for(bdd_context["run_id"]))
    assert chain[-1].comment is None


@then(parsers.parse("the response is a {status:d} naming the missing credential"))
def principal_required(bdd_context: dict[str, Any], status: int) -> None:
    response = bdd_context["response"]
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["error"] == "principal_required"
    assert bdd_context["settings"].principal_header in detail["detail"]


@then("no decision was recorded")
def nothing_recorded(bdd_context: dict[str, Any], run_async: Any) -> None:
    chain = run_async(bdd_context["store"].decisions_for(bdd_context["run_id"]))
    assert chain == []
