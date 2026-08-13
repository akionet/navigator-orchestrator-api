"""HTTP surface (SPEC-AIP-002 §3.8, AC-1/AC-7).

Two routes at R0: run a workflow (SSE) and report health. The route layer does
no validation of its own — the Runner owns the edge contracts, so a workflow
gets its 422 from its own `Input` model rather than from a hand-written check.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sse_starlette.sse import EventSourceResponse

from navigator_orchestrator.api.authz import CurrentPrincipal, RequiredPrincipal
from navigator_orchestrator.engine.policy import Policy, with_overrides
from navigator_orchestrator.engine.state import ContractError, serializable_errors
from navigator_orchestrator.engine.workflow import UnknownWorkflowError
from navigator_orchestrator.events import to_sse
from navigator_orchestrator.store import (
    RunConflictError,
    RunNotFoundError,
    RunRecord,
    RunState,
    Verdict,
)

__all__ = [
    "DecisionRequest",
    "ModelNotAllowedError",
    "WorkflowListResponse",
    "WorkflowSummary",
    "router",
]

router = APIRouter()


class ModelNotAllowedError(RuntimeError):
    """A client asked for a model outside `NAVIGATOR_ALLOWED_MODELS`."""


class WorkflowRunMismatchError(LookupError):
    """A known run was addressed through another workflow's URL."""

    def __init__(self, run_id: str, actual: str) -> None:
        self.run_id = run_id
        self.actual = actual
        super().__init__(f"run {run_id!r} belongs to workflow {actual!r}")


class WorkflowSummary(BaseModel):
    """Browser-safe metadata for one registered workflow."""

    model_config = ConfigDict(extra="forbid")

    name: str
    input_schema: dict[str, Any]
    source_kind: Literal["yaml", "python"]
    checkpointed: bool


class WorkflowListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflows: list[WorkflowSummary]


def _resolve_policy(context: Any, *, model: str | None, temperature: float | None) -> Policy:
    """Deployment policy + per-request overrides, re-validated.

    An unbounded client-supplied model is a cost risk, so a deployment can pin
    the set with `NAVIGATOR_ALLOWED_MODELS`. Left unset (the UAT default) any
    model is accepted, which is what makes model comparison cheap to run.
    """
    allowed = context.settings.allowed_models
    if model is not None and allowed and model not in allowed:
        raise ModelNotAllowedError(f"{model!r} is not in NAVIGATOR_ALLOWED_MODELS")
    return with_overrides(context.runner.default_policy, model=model, temperature=temperature)


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    """Engine + Postgres + Redis reachability (AC-7).

    Unconfigured dependencies report `disabled` rather than `unavailable`:
    a dev box with no Redis is healthy, a box that cannot reach its configured
    Redis is not.
    """
    context = request.app.state.context
    settings = context.settings

    redis_state = "disabled"
    if settings.redis_url:
        redis_state = "ok" if await context.cache.ping() else "unavailable"

    postgres_state = "disabled"
    if settings.database_url:
        postgres_state = "ok" if await _probe_postgres(settings.database_url) else "unavailable"

    body: dict[str, Any] = {
        "engine": {
            "state": "ok",
            "workflows": list(context.registry.names()),
            "model": settings.model,
        },
        "postgres": {"state": postgres_state},
        "redis": {"state": redis_state},
    }
    degraded = {postgres_state, redis_state} & {"unavailable"}
    return JSONResponse(body, status_code=503 if degraded else 200)


@router.get("/workflows", response_model=WorkflowListResponse)
async def list_workflows(request: Request) -> WorkflowListResponse:
    """Discover runnable workflows without treating `/healthz` as product API."""
    registry = request.app.state.context.registry
    workflows = [
        WorkflowSummary(
            name=workflow.name,
            input_schema=workflow.Input.model_json_schema(),
            source_kind=registry.source(workflow.name).kind,
            checkpointed=bool(workflow.checkpointed),
        )
        for workflow in registry
    ]
    workflows.sort(key=lambda workflow: workflow.name)
    return WorkflowListResponse(workflows=workflows)


@router.get("/workflows/{name}/source")
async def get_workflow_source(name: str, request: Request) -> Any:
    """Return only exact, trusted YAML captured at workflow registration."""
    registry = request.app.state.context.registry
    try:
        source = registry.source(name)
    except UnknownWorkflowError as exc:
        return JSONResponse({"error": "unknown_workflow", "detail": str(exc)}, status_code=404)
    if source.kind != "yaml" or source.text is None:
        return JSONResponse(
            {
                "error": "yaml_source_unavailable",
                "detail": f"workflow {name!r} is defined in Python, not YAML",
            },
            status_code=404,
        )
    return PlainTextResponse(source.text, media_type="application/yaml")


@router.post("/workflows/{name}/runs")
async def create_run(
    name: str,
    request: Request,
    principal: CurrentPrincipal,
    model: Annotated[
        str | None,
        Query(description="Override the deployment model, e.g. `vertex:gemini-3.5-pro`."),
    ] = None,
    temperature: Annotated[
        float | None,
        Query(ge=0.0, le=2.0, description="Sampling temperature; Gemini only."),
    ] = None,
) -> Any:
    """Start a run and stream `token`/`node`/`error`/`final` events.

    `model` and `temperature` are query params, not body fields: the body is
    the workflow's own `Input` contract (`extra="forbid"`), and *how* to run is
    a separate concern from *what* to run. Both are optional — omitting them
    uses the deployment's configured policy.
    """
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_json"}, status_code=400)

    context = request.app.state.context
    try:
        policy = _resolve_policy(context, model=model, temperature=temperature)
    except ModelNotAllowedError as exc:
        return JSONResponse({"error": "model_not_allowed", "detail": str(exc)}, status_code=403)
    except ValidationError as exc:
        return JSONResponse(
            {"error": "invalid_policy", "detail": serializable_errors(exc)},
            status_code=400,
        )

    runner = context.runner
    try:
        # Raises before the graph runs, so these are status codes, not events.
        stream = runner.run(name, payload, policy, principal=principal)
    except UnknownWorkflowError as exc:
        return JSONResponse({"error": "unknown_workflow", "detail": str(exc)}, status_code=404)
    except ContractError as exc:
        return JSONResponse(exc.as_payload(), status_code=422)

    return EventSourceResponse(
        _sse(stream),
        headers={"x-navigator-orchestrator-principal": principal.subject},
    )


class DecisionRequest(BaseModel):
    """A human's act on a paused run (SPEC-AIP-003 AC-3/AC-4)."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    #: Optional by design — approving without explanation is legitimate.
    comment: str | None = Field(default=None, max_length=4096)


@router.get("/workflows/{name}/runs")
async def list_runs(
    name: str,
    request: Request,
    state: Annotated[RunState | None, Query(description="e.g. awaiting_decision")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    """The reviewer queue (AC-2)."""
    context = request.app.state.context
    if context.run_store is None:
        return JSONResponse({"error": "runs_not_durable"}, status_code=501)
    runs = await context.run_store.list_runs(workflow=name, state=state, limit=limit)
    return {"runs": [r.model_dump(mode="json") for r in runs]}


@router.get("/workflows/{name}/runs/{run_id}")
async def get_run(name: str, run_id: str, request: Request) -> Any:
    """One run plus its decision chain — the audit view (AC-4)."""
    context = request.app.state.context
    if context.run_store is None:
        return JSONResponse({"error": "runs_not_durable"}, status_code=501)
    try:
        run = await _get_scoped_run(context, name, run_id)
        decisions = await context.run_store.decisions_for(run_id)
    except RunNotFoundError as exc:
        return JSONResponse({"error": "unknown_run", "detail": str(exc)}, status_code=404)
    except WorkflowRunMismatchError as exc:
        return _workflow_run_mismatch(exc)
    return {
        "run": run.model_dump(mode="json"),
        "decisions": [d.model_dump(mode="json") for d in decisions],
    }


@router.get("/workflows/{name}/runs/{run_id}/log")
async def get_run_log(name: str, run_id: str, request: Request) -> Any:
    """Ordered, summary-only execution transitions for one known run."""
    context = request.app.state.context
    if context.run_store is None:
        return JSONResponse({"error": "runs_not_durable"}, status_code=501)
    try:
        await _get_scoped_run(context, name, run_id)
    except RunNotFoundError as exc:
        return JSONResponse({"error": "unknown_run", "detail": str(exc)}, status_code=404)
    except WorkflowRunMismatchError as exc:
        return _workflow_run_mismatch(exc)
    entries = await context.run_log_store.read(run_id)
    return {"entries": [entry.model_dump(mode="json") for entry in entries]}


async def _get_scoped_run(context: Any, workflow: str, run_id: str) -> RunRecord:
    run: RunRecord = await context.run_store.get_run(run_id)
    if run.workflow != workflow:
        raise WorkflowRunMismatchError(run_id, run.workflow)
    return run


def _workflow_run_mismatch(exc: WorkflowRunMismatchError) -> JSONResponse:
    return JSONResponse(
        {"error": "workflow_run_mismatch", "detail": str(exc)},
        status_code=404,
    )


@router.post("/workflows/{name}/runs/{run_id}/decisions")
async def decide(  # noqa: PLR0911 - explicit HTTP outcomes keep decision semantics visible
    name: str,
    run_id: str,
    body: DecisionRequest,
    request: Request,
    principal: RequiredPrincipal,
) -> Any:
    """Resume a paused run with a decision (AC-3).

    The decision is recorded **before** the graph is re-entered, so a crash
    mid-resume leaves the audit trail intact and the run replayable. An audit
    record that depends on the workflow succeeding is not an audit record.
    """
    context = request.app.state.context
    if context.run_store is None:
        return JSONResponse({"error": "runs_not_durable"}, status_code=501)

    try:
        await _get_scoped_run(context, name, run_id)
        record, is_new = await context.run_store.append_decision(
            run_id, body.verdict, principal, body.comment
        )
    except RunNotFoundError as exc:
        return JSONResponse({"error": "unknown_run", "detail": str(exc)}, status_code=404)
    except WorkflowRunMismatchError as exc:
        return _workflow_run_mismatch(exc)
    except RunConflictError as exc:
        return JSONResponse(
            {"error": "run_conflict", "state": exc.state, "detail": str(exc)},
            status_code=409,
        )

    if not is_new:
        # A retry of the same decision by the same actor. The run already moved
        # on; replaying the graph would double side effects (AC-6).
        return JSONResponse(
            {"status": "already_decided", "decision": record.model_dump(mode="json")},
            status_code=200,
        )

    decision = {
        "verdict": record.verdict,
        "comment": record.comment,
        "decided_by": record.principal.subject,
    }
    try:
        stream = context.runner.resume(name, run_id, decision)
    except UnknownWorkflowError as exc:
        return JSONResponse({"error": "unknown_workflow", "detail": str(exc)}, status_code=404)

    return EventSourceResponse(
        _sse(stream), headers={"x-navigator-orchestrator-principal": principal.subject}
    )


async def _sse(stream: AsyncIterator[Any]) -> AsyncIterator[dict[str, str]]:
    async for event in stream:
        yield to_sse(event)


async def _probe_postgres(dsn: str) -> bool:
    try:
        import psycopg  # noqa: PLC0415 - optional extra, probe only
    except ImportError:
        return False
    try:
        async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=2) as conn:
            await conn.execute("SELECT 1")
    except Exception:
        return False
    return True
