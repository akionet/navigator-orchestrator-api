"""Slice 1 BDD for the React-admin operator contract (SPEC-WFB-002)."""

from __future__ import annotations

from typing import Any

from conftest import parse_sse, running_app
from pytest_bdd import given, parsers, scenarios, then, when

from navigator_orchestrator.api.app import build_app
from navigator_orchestrator.engine.checkpoint import make_memory_checkpointer
from navigator_orchestrator.engine.workflow import WorkflowRegistry, WorkflowSource
from navigator_orchestrator.store import InMemoryRunLogStore, InMemoryRunStore, Principal
from navigator_orchestrator.workflows.echo import EchoWorkflow

scenarios("operator-console-api.feature")

YAML_SOURCE = """name: echo
steps:
  - id: echo
    uses: core.echo
"""
TEST_CONTENT_SENTINEL = "stream-content-must-not-be-persisted"


@given(parsers.parse('a runtime with a YAML-backed "{name}" workflow'))
def yaml_runtime(
    name: str,
    settings: Any,
    fake_llm: Any,
    cache: Any,
    observability: Any,
    bdd_context: dict[str, Any],
) -> None:
    assert name == "echo"
    registry = WorkflowRegistry()
    registry.register(
        EchoWorkflow(),
        source=WorkflowSource(kind="yaml", logical_name="flows/echo.yaml", text=YAML_SOURCE),
    )
    bdd_context["app"] = build_app(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        registry=registry,
    )
    bdd_context["yaml"] = YAML_SOURCE


@given("the default code-defined workflows")
def code_runtime(app: Any, bdd_context: dict[str, Any]) -> None:
    bdd_context["app"] = app


@given("a runtime with previous runs across workflows and states")
def history_runtime(
    settings: Any,
    fake_llm: Any,
    cache: Any,
    observability: Any,
    bdd_context: dict[str, Any],
    run_async: Any,
) -> None:
    run_store = InMemoryRunStore()
    log_store = InMemoryRunLogStore()

    async def _seed() -> None:
        actor = Principal(subject="operator@example.com", issuer="header")
        await run_store.create_run("echo-001", "echo", {"model": "fake:echo"}, actor)
        await run_store.mark_state("echo-001", "completed")
        await run_store.create_run("echo-002", "echo", {"model": "fake:echo"}, actor)
        await run_store.mark_state("echo-002", "completed")
        await run_store.create_run("echo-003", "echo", {"model": "fake:echo"}, actor)
        await run_store.mark_state("echo-003", "failed")
        await run_store.create_run("approval-waiting", "approval", {}, actor)
        await run_store.mark_state(
            "approval-waiting", "awaiting_decision", {"proposal": "review me"}
        )
        await log_store.append(run_id="echo-002", workflow="echo", step="echo", status="started")
        await log_store.append(run_id="echo-002", workflow="echo", status="completed")

    run_async(_seed())
    bdd_context["app"] = build_app(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        run_store=run_store,
        run_log_store=log_store,
    )


@given("a runtime with a resumable approval workflow")
def resumable_approval_runtime(
    settings: Any,
    fake_llm: Any,
    cache: Any,
    observability: Any,
    bdd_context: dict[str, Any],
) -> None:
    bdd_context["app"] = build_app(
        settings,
        llm=fake_llm,
        cache=cache,
        observability=observability,
        run_store=InMemoryRunStore(),
        run_log_store=InMemoryRunLogStore(),
        checkpointer=make_memory_checkpointer(),
    )


@when(parsers.parse('the console discovers workflows and starts "{name}" with valid JSON'))
def discover_and_start(
    name: str,
    bdd_context: dict[str, Any],
    run_async: Any,
) -> None:
    async def _call() -> None:
        async with running_app(bdd_context["app"]) as client:
            bdd_context["discovery"] = await client.get("/workflows")
            bdd_context["run"] = await client.post(
                f"/workflows/{name}/runs", json={"text": "from-console"}
            )

    run_async(_call())


@when(parsers.parse('the console requests the "{name}" workflow source'))
def request_source(name: str, bdd_context: dict[str, Any], run_async: Any) -> None:
    async def _call() -> None:
        async with running_app(bdd_context["app"]) as client:
            bdd_context["source"] = await client.get(f"/workflows/{name}/source")
            bdd_context["path_probe"] = await client.get(
                f"/workflows/{name}/source", params={"path": "../../.env"}
            )

    run_async(_call())


@when(parsers.parse('the console starts "{name}" and requests its execution log'))
def start_and_request_log(name: str, bdd_context: dict[str, Any], run_async: Any) -> None:
    async def _call() -> None:
        async with running_app(bdd_context["app"]) as client:
            response = await client.post(
                f"/workflows/{name}/runs", json={"text": TEST_CONTENT_SENTINEL}
            )
            events = parse_sse(response.text)
            run_id = events[0]["data"]["run_id"]
            bdd_context["log"] = await client.get(f"/workflows/{name}/runs/{run_id}/log")
            bdd_context["mismatch"] = await client.get(f"/workflows/approval/runs/{run_id}/log")

    run_async(_call())


@when(parsers.parse('the console filters completed "{name}" runs and opens their detail'))
def filter_and_open_history(name: str, bdd_context: dict[str, Any], run_async: Any) -> None:
    async def _call() -> None:
        async with running_app(bdd_context["app"]) as client:
            bdd_context["history"] = await client.get(
                f"/workflows/{name}/runs", params={"state": "completed", "limit": 1}
            )
            run_id = bdd_context["history"].json()["runs"][0]["id"]
            bdd_context["detail"] = await client.get(f"/workflows/{name}/runs/{run_id}")
            bdd_context["history_log"] = await client.get(f"/workflows/{name}/runs/{run_id}/log")
            bdd_context["detail_mismatch"] = await client.get(f"/workflows/approval/runs/{run_id}")

    run_async(_call())


@when("the console starts approves and retries the approval run")
def approve_and_retry(bdd_context: dict[str, Any], run_async: Any) -> None:
    async def _call() -> None:
        async with running_app(bdd_context["app"]) as client:
            started = await client.post("/workflows/approval/runs", json={"text": "ship it"})
            run_id = parse_sse(started.text)[-1]["data"]["run_id"]
            bdd_context["decision_mismatch"] = await client.post(
                f"/workflows/echo/runs/{run_id}/decisions",
                json={"verdict": "approve", "comment": "looks good"},
            )
            bdd_context["decision"] = await client.post(
                f"/workflows/approval/runs/{run_id}/decisions",
                json={"verdict": "approve", "comment": "looks good"},
            )
            bdd_context["decision_retry"] = await client.post(
                f"/workflows/approval/runs/{run_id}/decisions",
                json={"verdict": "approve", "comment": "looks good"},
            )
            bdd_context["decision_detail"] = await client.get(f"/workflows/approval/runs/{run_id}")
            bdd_context["decision_log"] = await client.get(f"/workflows/approval/runs/{run_id}/log")
            bdd_context["decision_run_id"] = run_id

    run_async(_call())


@then(parsers.parse('discovery describes the "{name}" input contract'))
def discovery_describes_input(name: str, bdd_context: dict[str, Any]) -> None:
    response = bdd_context["discovery"]
    assert response.status_code == 200
    workflows = {workflow["name"]: workflow for workflow in response.json()["workflows"]}
    summary = workflows[name]
    assert summary["source_kind"] == "yaml"
    assert summary["input_schema"]["required"] == ["text"]
    assert summary["input_schema"]["properties"]["text"]["type"] == "string"


@then("the run completes through the existing SSE contract")
def run_completes(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["run"]
    assert response.status_code == 200
    events = parse_sse(response.text)
    final = next(event for event in events if event["event"] == "final")
    assert final["data"]["output"]["text"] == "from-console"


@then("the response is the exact registered YAML")
def exact_yaml(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["source"]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert response.text == bdd_context["yaml"]


@then("no filesystem path was accepted from the console")
def no_path_lookup(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["path_probe"]
    assert response.status_code == 200
    assert response.text == bdd_context["yaml"]


@then(parsers.parse('the response is {status:d} with "{error}"'))
def response_error(status: int, error: str, bdd_context: dict[str, Any]) -> None:
    response = bdd_context["source"]
    assert response.status_code == status
    assert response.json()["error"] == error


@then("the execution log contains ordered node and terminal summaries")
def ordered_execution_log(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["log"]
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert [entry["seq"] for entry in entries] == list(range(1, len(entries) + 1))
    assert entries[-1]["status"] == "completed"
    assert any(entry["status"] == "started" for entry in entries)
    assert any(entry["status"] == "completed" and entry["step"] for entry in entries)
    assert bdd_context["mismatch"].status_code == 404
    assert bdd_context["mismatch"].json()["error"] == "workflow_run_mismatch"


@then("the execution log does not persist streamed or final content")
def summary_only_execution_log(bdd_context: dict[str, Any]) -> None:
    assert TEST_CONTENT_SENTINEL not in bdd_context["log"].text


@then("only the newest matching run is listed")
def newest_matching_history(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["history"]
    assert response.status_code == 200
    assert [run["id"] for run in response.json()["runs"]] == ["echo-002"]


@then("its metadata decisions and ordered log come from the runtime")
def history_detail_and_log(bdd_context: dict[str, Any]) -> None:
    detail = bdd_context["detail"].json()
    assert detail["run"]["id"] == "echo-002"
    assert detail["run"]["created_by"]["subject"] == "operator@example.com"
    assert detail["decisions"] == []
    entries = bdd_context["history_log"].json()["entries"]
    assert [entry["seq"] for entry in entries] == [1, 2]


@then("another workflow cannot open that run")
def history_workflow_mismatch(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["detail_mismatch"]
    assert response.status_code == 404
    assert response.json()["error"] == "workflow_run_mismatch"


@then("the decision resumes the same run as an event stream")
def decision_resumes_same_run(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["decision"]
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert events[-1]["event"] == "final"
    assert {event["data"]["run_id"] for event in events} == {bdd_context["decision_run_id"]}
    retry = bdd_context["decision_retry"]
    assert retry.headers["content-type"].startswith("application/json")
    assert retry.json()["status"] == "already_decided"


@then("the refreshed audit has one decision and a continued ordered log")
def decision_audit_and_log(bdd_context: dict[str, Any]) -> None:
    detail = bdd_context["decision_detail"].json()
    assert detail["run"]["state"] == "completed"
    assert len(detail["decisions"]) == 1
    entries = bdd_context["decision_log"].json()["entries"]
    assert [entry["seq"] for entry in entries] == list(range(1, len(entries) + 1))
    assert any(entry["status"] == "awaiting_decision" for entry in entries)
    assert entries[-1]["status"] == "completed"


@then("a wrong workflow could not write a decision")
def wrong_workflow_wrote_nothing(bdd_context: dict[str, Any]) -> None:
    response = bdd_context["decision_mismatch"]
    assert response.status_code == 404
    assert response.json()["error"] == "workflow_run_mismatch"
    assert len(bdd_context["decision_detail"].json()["decisions"]) == 1
