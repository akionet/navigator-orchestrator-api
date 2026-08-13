"""Execute a template in-process (P0).

Deliberately the smallest thing that runs a template end to end: steps in
declared order, results accumulating in a pool, hooks bound by name. There is
no store, no queue, no worker and no HTTP — that split is P2, and the `agent`
executor moving onto LangGraph is P4.

What is already load-bearing, and must survive those phases unchanged:

- the **author writes no control flow** — ordering is the template's business;
- a hook returns data and never performs an effect;
- binding is by name over a superset, so a template may grow kwargs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from navigator_orchestrator.sdk.binding import bind_kwargs
from navigator_orchestrator.sdk.context import Blocked, Ctx
from navigator_orchestrator.sdk.execution import (
    StepFailed,
    call_step_hook,
    resolve_hook,
    run_engine_step,
    summarise_product,
)
from navigator_orchestrator.sdk.preflight import describe_missing, missing_requirements
from navigator_orchestrator.sdk.service import backend_requirements
from navigator_orchestrator.sdk.templates import ENGINE_IMPLEMENTED, Template
from navigator_orchestrator.store.events import EventLog, EventStatus, NullEventLog, StepEvent

# `StepFailed` is re-exported: it moved to `execution.py`, and callers that
# imported it from here should not have to care.
__all__ = ["RunResult", "StepFailed", "StepRecord", "new_run_id", "run_template"]


@dataclass(frozen=True, slots=True)
class StepRecord:
    """One executed step. The seed of P5's ledger."""

    step: str
    executor: str
    source: str  # "file" when the author overrode it, "default" otherwise
    produced: str


@dataclass(slots=True)
class RunResult:
    """Everything the run produced, plus how it got there."""

    workflow: str
    run_id: str = ""
    pool: dict[str, Any] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Set when the run stopped at a gate instead of finishing. The process may
    #: exit here: the checkpointer holds everything needed to resume.
    paused_at: str = ""
    #: What a reviewer is shown. Opaque to the engine (SPEC-AIP-003 §3.3).
    gate: dict[str, Any] | None = None

    @property
    def is_paused(self) -> bool:
        return bool(self.paused_at)

    @property
    def output(self) -> Any:
        """The last step's product — what a caller usually wants."""
        return self.pool.get(self.steps[-1].produced) if self.steps else None


def new_run_id(workflow: str) -> str:
    """Sortable and readable: a listing is chronological without parsing a date."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{workflow}-{uuid.uuid4().hex[:6]}"


async def run_template(
    template: Template,
    hooks: Mapping[str, Callable[..., Any]],
    ctx: Ctx,
    *,
    events: EventLog | None = None,
    run_id: str = "",
    actor: str = "system",
) -> RunResult:
    """Run every step in order, threading results through the pool.

    The event log is written **here**, not by the steps (SPEC-EDW-002 §5). A
    template author therefore cannot forget to log, and cannot log a step that
    did not run. Each step emits `started` and then exactly one terminal row.
    """
    log = events or NullEventLog()
    run_id = run_id or new_run_id(template.name)
    result = RunResult(workflow=template.name, run_id=run_id)
    pool: dict[str, Any] = dict(ctx.params)
    seq = 0

    def record(step_name: str, status: EventStatus, **detail: Any) -> None:
        nonlocal seq
        seq += 1
        log.append(
            StepEvent(
                run_id=run_id,
                seq=seq,
                workflow=template.name,
                step=step_name,
                status=status,
                actor=actor,
                detail=detail,
            )
        )

    record("run", "started", steps=len(template.steps))

    # Before anything expensive. A missing credential should cost nothing.
    # A `service` step's credential is a requirement the *template* never
    # declared — it comes from the manifest — so it is folded in here rather
    # than asking every author to remember it (SPEC-NSP-006 §5).
    required = (*template.requires, *backend_requirements(template, ctx.project))
    absent = missing_requirements(required)
    if absent:
        record("preflight", "blocked", missing=[r.name for r in absent])
        record("run", "blocked", at_step="preflight")
        raise Blocked(describe_missing(absent))
    if required:
        record("preflight", "ok", checked=len(required))

    for step in template.steps:
        if step.executor in ENGINE_IMPLEMENTED and step.executor != "gate":
            fn, source, kwargs = None, "engine", {}
        else:
            fn, source = resolve_hook(step, hooks)
            kwargs = bind_kwargs(fn, available=pool, allowed=step.kwargs)
        record(step.name, "started", executor=step.executor, source=source)

        # Cleared before each step, read after: `ctx` outlives one step, so a
        # skip left set would mark every later step skipped too.
        ctx.skipped = ""
        ctx.detail = {}
        try:
            produced = (
                await run_engine_step(step, ctx, pool)
                if fn is None
                else await call_step_hook(step, fn, ctx, kwargs)
            )
        except Blocked as exc:
            # A rule said no. Distinct from `failed`: the workflow worked, the
            # content did not qualify, and the remedy is different.
            record(step.name, "blocked", reason=str(exc)[:300], **ctx.detail)
            record("run", "blocked", at_step=step.name)
            raise
        except Exception as exc:
            record(step.name, "failed", error=type(exc).__name__, detail=str(exc)[:300])
            record("run", "failed", at_step=step.name)
            raise StepFailed(step.name, exc) from exc

        pool[step.produces] = produced
        # `skipped` is a real outcome, not an absence — SPEC-EDW-002 §4. It is
        # how "which records predate the KG" stays a query rather than
        # archaeology. The *hook* says so via `ctx.skip`; the engine used to
        # infer it from a record field name, which meant any other domain's
        # skipped step was silently recorded as `ok` (SPEC-NSP-005 §3).
        # `ctx.detail` is how the step went; `summarise_product` is what it
        # produced. Both belong in the row, and neither substitutes for the other.
        detail = {**summarise_product(produced, step.summary_keys), **ctx.detail}
        status: EventStatus = "ok"
        if ctx.skipped:
            status, detail = "skipped", {**detail, "reason": ctx.skipped[:300]}
        record(step.name, status, **detail)
        result.steps.append(
            StepRecord(
                step=step.name,
                executor=step.executor,
                source=source,
                produced=step.produces,
            )
        )

    result.pool = pool
    result.notes = list(ctx.notes)
    record("run", "ok", steps=len(result.steps))
    return result
