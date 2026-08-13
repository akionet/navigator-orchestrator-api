"""Compile a template into a LangGraph (SPEC-NSP-003 §2.3, PLAN G1b).

The sequential runner has nothing to interrupt: a `for` loop cannot be paused
and resumed in another process. A `StateGraph` can, because LangGraph already
solved that — `interrupt()`, `Command(resume=…)` and a checkpointer.

This stage builds the graph and proves it produces exactly what the sequential
runner produces. **Gates and checkpointing are G1c**; introducing them at the
same time as a new execution engine would mean two suspects for every failure.

## The state, and why the reducers matter

LangGraph merges what each node returns into the running state through a
*reducer*. Get one wrong and the result is plausible rather than broken — a
note recorded once per step instead of once, a pool key silently replaced. So:

- `pool` merges (`{**old, **new}`) — later steps add products, never erase
  earlier ones.
- `notes` concatenates — each node contributes only what its hook appended.

`tests/test_graph_runner.py` compares both runners rather than trusting the
above, and was written before this file for exactly that reason.

## What does not change

Hooks are still `(ctx, **kwargs) -> data`, bound by name, and entirely ignorant
that a graph exists. No template and no workflow file was edited to make this
work — which is the test of whether the authoring interface was drawn in the
right place.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from navigator_orchestrator.sdk.binding import bind_kwargs
from navigator_orchestrator.sdk.context import Blocked, Ctx
from navigator_orchestrator.sdk.execution import (
    StepFailed,
    call_step_hook,
    resolve_hook,
    run_engine_step,
    summarise_product,
)
from navigator_orchestrator.sdk.judge import Judge, JudgeError, run_judge
from navigator_orchestrator.sdk.preflight import describe_missing, missing_requirements
from navigator_orchestrator.sdk.runner import RunResult, StepRecord, new_run_id
from navigator_orchestrator.sdk.service import backend_requirements
from navigator_orchestrator.sdk.templates import ENGINE_IMPLEMENTED, Step, Template
from navigator_orchestrator.store.events import EventLog, EventStatus, NullEventLog, StepEvent

__all__ = [
    "GraphState",
    "build_graph",
    "gate_payload_of",
    "resume_template_graph",
    "run_template_graph",
]


def gate_payload_of(step: Step, pool: Mapping[str, Any]) -> dict[str, Any]:
    """What a reviewer is shown when the run stops here.

    Opaque to the engine by design (`SPEC-AIP-003` §3.3): the *template* decides,
    by declaring the pool keys it wants surfaced as the step's `kwargs`. Core
    never interprets it, which is why one CLI can review any workflow.
    """
    return {
        "step": step.name,
        "doc": step.doc,
        "payload": {key: pool[key] for key in step.kwargs if key in pool},
    }


#: Distinguishes "the pool has no such path" from "the value there is falsy".
#: Collapsing the two would make a typo in `when=` silently disable a gate.
_UNRESOLVED = object()


def _resolve_path(pool: Mapping[str, Any], path: str) -> Any:
    """Walk a dotted pool path, or return `_UNRESOLVED`."""
    current: Any = pool
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return _UNRESOLVED
    return current


def gate_is_required(step: Step, pool: Mapping[str, Any]) -> bool:
    """Whether this gate is material for this run (`Step.when`).

    A gate with no condition always pauses — the safe default, and the prior
    behaviour. With a condition, the run stops only when the named value is
    truthy, so a compliance officer is asked about the PEP match that exists
    rather than confirming eight times a day that there isn't one.

    **Fails closed.** A condition naming a path no step produced — a typo, a
    renamed `produces`, a step that was removed — pauses rather than skips.
    Skipping would silently drop a compliance control and leave a clean record
    behind it, which is the worst of the available failure modes.

    The skip is recorded as a decision either way, so "no human was asked" is a
    fact in the audit trail rather than an absence in it.
    """
    if not step.when:
        return True
    value = _resolve_path(pool, step.when)
    if value is _UNRESOLVED:
        return True
    return bool(value)


def _merge_pool(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Later products are added; earlier ones survive."""
    return {**left, **right}


def _extend(left: list[Any], right: list[Any]) -> list[Any]:
    return [*left, *right]


class GraphState(TypedDict, total=False):
    """What a checkpointer will persist at G1c.

    Kept flat and JSON-ish on purpose: a checkpointer has to serialise it, so a
    step returning a handle rather than data cannot survive a pause. That is
    LangGraph's constraint, not one invented here, and it pushes in a direction
    already wanted.
    """

    pool: Annotated[dict[str, Any], _merge_pool]
    notes: Annotated[list[str], _extend]
    steps: Annotated[list[dict[str, str]], _extend]


class _Recorder:
    """Event-log emission with a sequence shared across nodes.

    A plain closure over an `int` would not do — nodes rebind rather than
    mutate — so the counter lives on an object.
    """

    def __init__(self, log: EventLog, run_id: str, workflow: str, actor: str) -> None:
        self._log, self._run_id = log, run_id
        self._workflow, self._actor = workflow, actor
        self.seq = 0

    def __call__(self, step: str, status: EventStatus, **detail: Any) -> None:
        self.seq += 1
        self._log.append(
            StepEvent(
                run_id=self._run_id,
                seq=self.seq,
                workflow=self._workflow,
                step=step,
                status=status,
                actor=self._actor,
                detail=detail,
            )
        )


def _make_node(
    step: Step,
    hooks: Mapping[str, Callable[..., Any]],
    ctx: Ctx,
    record: _Recorder,
) -> Callable[[GraphState], Any]:
    async def node(state: GraphState) -> dict[str, Any]:
        pool = dict(state.get("pool") or {})

        if step.executor == "gate":
            if not gate_is_required(step, pool):
                # Recorded, not silent: the audit trail says a human was not
                # asked and why, which is a different fact from a gate that
                # never existed.
                return {
                    "pool": {
                        step.produces: {
                            "verdict": "not_required",
                            "reason": f"{step.when} is not set for this run",
                        }
                    },
                    "steps": [
                        {
                            "step": step.name,
                            "executor": step.executor,
                            "source": "engine",
                            "produced": step.produces,
                        }
                    ],
                }
            # Everything before this is already checkpointed, so the process may
            # now exit. `interrupt` is what makes that safe.
            #
            # No event is recorded here. LangGraph re-executes this node on
            # resume — `interrupt` raises the first time and returns the verdict
            # the second — so recording inside would log every pause twice. The
            # caller records instead, where pausing and resuming are
            # distinguishable.
            verdict = interrupt(gate_payload_of(step, pool))
            return {
                "pool": {step.produces: verdict},
                "steps": [
                    {
                        "step": step.name,
                        "executor": step.executor,
                        "source": "human",
                        "produced": step.produces,
                    }
                ],
            }

        # `gate` returned above, so what is left of ENGINE_IMPLEMENTED here is
        # `shell` and `service` — both dispatched, neither needing a hook.
        if step.executor in ENGINE_IMPLEMENTED:
            fn, source, kwargs = None, "engine", {}
        else:
            fn, source = resolve_hook(step, hooks)
            kwargs = bind_kwargs(fn, available=pool, allowed=step.kwargs)
        record(step.name, "started", executor=step.executor, source=source)

        # Hooks append to `ctx.notes`; take only what this step added, so the
        # reducer concatenates rather than re-adding the whole list each time.
        before = len(ctx.notes)
        ctx.skipped = ""
        ctx.detail = {}
        try:
            produced = (
                await run_engine_step(step, ctx, pool)
                if fn is None
                else await call_step_hook(step, fn, ctx, kwargs)
            )
        except Blocked as exc:
            record(step.name, "blocked", reason=str(exc)[:300], **ctx.detail)
            record("run", "blocked", at_step=step.name)
            raise
        except Exception as exc:
            record(step.name, "failed", error=type(exc).__name__, detail=str(exc)[:300])
            record("run", "failed", at_step=step.name)
            raise StepFailed(step.name, exc) from exc

        # `ctx.skip` rather than a field-name sniff — see `run_template` for why
        # the engine had no business knowing what `enrichment` was.
        # `ctx.detail` is how the step went; `summarise_product` is what it
        # produced. Both belong in the row, and neither substitutes for the other.
        detail = {**summarise_product(produced, step.summary_keys), **ctx.detail}
        status: EventStatus = "ok"
        if ctx.skipped:
            status, detail = "skipped", {**detail, "reason": ctx.skipped[:300]}
        record(step.name, status, **detail)
        return {
            "pool": {step.produces: produced},
            "notes": ctx.notes[before:],
            "steps": [
                {
                    "step": step.name,
                    "executor": step.executor,
                    "source": source,
                    "produced": step.produces,
                }
            ],
        }

    return node


def build_graph(
    template: Template,
    hooks: Mapping[str, Callable[..., Any]],
    ctx: Ctx,
    record: _Recorder,
    judges: Sequence[Judge] = (),
) -> Any:
    """A `StateGraph` mirroring the template's step order.

    Returned uncompiled so the caller supplies the checkpointer — which is what
    keeps `sqlite` and `postgres` a deployment choice rather than a code path.
    """
    graph: Any = StateGraph(GraphState)
    _check_placements(template, judges)
    previous: str = START
    for step in template.steps:
        # A judge attached `before:` this step goes in front of it — which is
        # what makes "judge the candidate before it is published" a placement
        # rather than a template edit (SPEC-NSP-004 §4.2).
        for judge in _attached(judges, "before", step.name):
            graph.add_node(judge.node_name, _make_judge_node(judge, ctx, record))
            graph.add_edge(previous, judge.node_name)
            previous = judge.node_name

        graph.add_node(step.name, _make_node(step, hooks, ctx, record))
        graph.add_edge(previous, step.name)
        previous = step.name

        for judge in _attached(judges, "after", step.name):
            graph.add_node(judge.node_name, _make_judge_node(judge, ctx, record))
            graph.add_edge(previous, judge.node_name)
            previous = judge.node_name

    graph.add_edge(previous, END)
    return graph


def _attached(judges: Sequence[Judge], where: str, step_name: str) -> list[Judge]:
    return [j for j in judges if step_name in getattr(j, where)]


def _check_placements(template: Template, judges: Sequence[Judge]) -> None:
    """Every judge must attach to a step the template actually declares.

    **The templates still own the DAG** (`SPEC-NSP-004` §4.2). A judge supplies
    configuration, never structure, so naming a step that does not exist is an
    error with the valid names listed — not a judge that silently never runs,
    which is the worst outcome for a compliance gate: it reports as configured
    and guards nothing.
    """
    names = set(template.hook_names())

    # Two judges sharing an `id` — `sanctions@1` and `sanctions@2`, the ordinary result
    # of versioning a prompt — would both be a node called `judge-sanctions`, and
    # `add_node` on an existing name *replaces* it. One compliance gate would
    # silently vanish and the run would look identical. Bumping a version is
    # supposed to be the safe move, so it must not be the one that disarms a
    # judge (`SPEC-NSP-004` §5).
    seen: dict[str, Judge] = {}
    for judge in judges:
        clash = seen.get(judge.node_name)
        if clash is not None:
            raise JudgeError(
                f"judges {clash.ref} and {judge.ref} would both be node "
                f"{judge.node_name!r}. Two versions of one judge cannot both "
                f"guard a run: retire the old file, or give one a different id."
            )
        seen[judge.node_name] = judge

    for judge in judges:
        # At least one anchor must exist, not all of them: a judge naming
        # both `store` and `publish` is saying "whichever of these this flow
        # writes with", and no flow has both.
        if not names.intersection(judge.anchors):
            valid = ", ".join(template.hook_names()) or "none"
            raise JudgeError(
                f"judge {judge.ref} attaches before/after {', '.join(judge.anchors)}, "
                f"none of which {template.name!r} has (steps: {valid})"
            )
        if judge.node_name in names:
            raise JudgeError(
                f"judge {judge.ref} would take the node name {judge.node_name!r}, "
                f"which is already a step in {template.name!r}; rename one"
            )


def _make_judge_node(judge: Judge, ctx: Ctx, record: _Recorder) -> Callable[[GraphState], Any]:
    """A judge is an **ordinary node**.

    Deliberately built the same way a step is, so it inherits event logging,
    failure handling and checkpointing rather than having them reimplemented
    beside it. `SPEC-NSP-004` §4.2 asks for exactly this: not a special case
    bolted on the side.
    """

    async def node(state: GraphState) -> dict[str, Any]:
        pool = dict(state.get("pool") or {})
        record(judge.node_name, "started", executor="judge", source=judge.ref)
        before = len(ctx.notes)
        ctx.skipped, ctx.detail = "", {}

        try:
            verdict = await run_judge(judge, ctx, pool)
        except Blocked as exc:
            # The judge said no, or could not say anything. Both are `blocked`
            # rather than `failed`: the workflow worked, the content did not
            # qualify, and the remedy is a different one.
            record(judge.node_name, "blocked", reason=str(exc)[:300], **ctx.detail)
            record("run", "blocked", at_step=judge.node_name)
            raise
        except Exception as exc:  # pragma: no cover - run_judge funnels to Blocked
            record(judge.node_name, "failed", error=type(exc).__name__, detail=str(exc)[:300])
            record("run", "failed", at_step=judge.node_name)
            raise StepFailed(judge.node_name, exc) from exc

        record(judge.node_name, "ok", **{**verdict.summary(), **ctx.detail})
        return {
            "pool": {f"verdict_{judge.id}": verdict.summary()},
            "notes": ctx.notes[before:],
            "steps": [
                {
                    "step": judge.node_name,
                    "executor": "judge",
                    "source": judge.ref,
                    "produced": f"verdict_{judge.id}",
                }
            ],
        }

    return node


async def run_template_graph(
    template: Template,
    hooks: Mapping[str, Callable[..., Any]],
    ctx: Ctx,
    *,
    events: EventLog | None = None,
    run_id: str = "",
    actor: str = "system",
    checkpointer: Any = None,
    judges: Sequence[Judge] = (),
) -> RunResult:
    """Run a template through LangGraph.

    Same contract as `run_template`, plus: with a checkpointer, a `gate` step
    pauses the run and returns a `RunResult` with `is_paused` set. Without one,
    a gate would have nowhere to pause to — see `SPEC-NSP-003` §2.2.1.
    """
    log = events or NullEventLog()
    run_id = run_id or new_run_id(template.name)
    record = _Recorder(log, run_id, template.name, actor)

    record("run", "started", steps=len(template.steps))

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

    compiled = build_graph(template, hooks, ctx, record, judges).compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run_id}}
    final: dict[str, Any] = await compiled.ainvoke({"pool": dict(ctx.params)}, config=config)
    return _finish(final, template, run_id, record)


async def resume_template_graph(
    template: Template,
    hooks: Mapping[str, Callable[..., Any]],
    ctx: Ctx,
    *,
    verdict: dict[str, Any],
    events: EventLog | None = None,
    run_id: str,
    actor: str = "system",
    checkpointer: Any = None,
    judges: Sequence[Judge] = (),
) -> RunResult:
    """Continue a paused run. Usually a different process from the one that
    started it — which is the entire point of checkpointing.

    The graph is rebuilt from the same template; the *state* comes from the
    checkpointer, keyed by `thread_id`. `ctx` is fresh, and deliberately so:
    notes accumulated before the pause live in graph state, so a new `ctx`
    contributes only what this leg adds and the reducer concatenates.
    """
    if checkpointer is None:
        raise Blocked(
            f"run {run_id} cannot be resumed: it was recorded without a "
            f"checkpointer, so there is no state to continue from. Runs started "
            f"with --block are never resumable (SPEC-NSP-003 §2.2.1)."
        )
    log = events or NullEventLog()
    record = _Recorder(log, run_id, template.name, actor)
    record("run", "resumed", verdict=verdict.get("verdict", "approve"))

    compiled = build_graph(template, hooks, ctx, record, judges).compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run_id}}
    final: dict[str, Any] = await compiled.ainvoke(Command(resume=verdict), config=config)
    return _finish(final, template, run_id, record)


def _finish(final: dict[str, Any], template: Template, run_id: str, record: _Recorder) -> RunResult:
    """Turn a graph result into a `RunResult`, paused or complete."""
    result = RunResult(
        workflow=template.name,
        run_id=run_id,
        pool=dict(final.get("pool") or {}),
        steps=[StepRecord(**row) for row in (final.get("steps") or [])],
        notes=list(final.get("notes") or []),
    )

    # LangGraph reports a pause by putting the outstanding interrupts in the
    # result rather than by raising, so a paused run is not a failure and must
    # not be reported as one.
    pending = final.get("__interrupt__")
    if pending:
        payload = getattr(pending[0], "value", None) or {}
        result.paused_at = str(payload.get("step") or "gate")
        result.gate = payload
        # A summary, not the payload — SPEC-EDW-002 §5. Splatting it here also
        # collided with the recorder's own `step` parameter, which was the bug
        # that surfaced the rule.
        record(
            result.paused_at,
            "awaiting_human",
            doc=payload.get("doc", ""),
            keys=sorted(payload.get("payload") or {}),
        )
        record("run", "awaiting_human", at_step=result.paused_at)
        return result

    record("run", "ok", steps=len(result.steps))
    return result
