"""Executing one step, independent of what drives the sequence.

Extracted from `sdk/runner.py` (PLAN-NSP-R2-003 G1a) so that the sequential
runner and the LangGraph runner share these rather than one reaching into the
other's privates. Nothing here knows whether it is being driven by a `for` loop
or a compiled graph, which is the property that lets both exist.

Public on purpose. The first attempt at the graph runner imported `_call`,
`_implementation` and `_summarise` across module boundaries — making a module's
internals load-bearing elsewhere while still signalling "do not depend on this".
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from navigator_orchestrator.sdk.context import Ctx
from navigator_orchestrator.sdk.registry import UnknownImplementationError, resolve_uses
from navigator_orchestrator.sdk.templates import Step

__all__ = [
    "StepFailed",
    "call_hook",
    "call_step_hook",
    "resolve_hook",
    "run_engine_step",
    "summarise_product",
]


class StepFailed(Exception):
    """A step raised. Carries which one, because a bare traceback does not."""

    def __init__(self, step: str, cause: BaseException) -> None:
        self.step = step
        self.cause = cause
        super().__init__(f"step {step!r} failed: {cause}")


async def run_engine_step(step: Step, ctx: Ctx, pool: dict[str, Any]) -> Any:
    """Execute a step the engine implements itself — `shell` or `service`.

    Shared by both runners. The sequential and graph runners each had their own
    `if step.executor == "shell"` branch, and a second executor would have made
    that a second divergence waiting to happen; `PLAN-NSP-R2-006` §4 named this
    as a falsification signal, so it is collapsed at the second rather than the
    third.
    """
    # Imported here, not at module scope: `service` pulls in httpx, and a
    # workflow that never calls an API should not pay for a transport.
    if step.executor == "shell":
        from navigator_orchestrator.sdk.shell import run_shell_step  # noqa: PLC0415

        return await run_shell_step(step, ctx, pool)

    if step.executor == "validate":
        from navigator_orchestrator.sdk.context import Blocked  # noqa: PLC0415
        from navigator_orchestrator.sdk.schema import validate_instance  # noqa: PLC0415
        from navigator_orchestrator.sdk.schema_sources import load_locked_schema  # noqa: PLC0415

        if ctx.project is None:
            raise RuntimeError(f"validate step {step.name!r} needs a workflow project")
        if step.input not in pool:
            raise RuntimeError(f"validate step {step.name!r} cannot find pool input {step.input!r}")
        result = validate_instance(load_locked_schema(ctx.project, step.schema), pool[step.input])
        ctx.detail = {
            "schema_id": result.schema_id,
            "schema_revision": result.revision,
            "valid": result.valid,
            "finding_count": len(result.findings),
            "finding_paths": [finding.path for finding in result.findings[:8]],
        }
        if not result.valid:
            first = result.findings[0]
            raise Blocked(
                f"schema {result.schema_id!r} rejected {step.input!r} at "
                f"{first.path or '/'}: {first.message}"
            )
        return result

    from navigator_orchestrator.sdk.service import (  # noqa: PLC0415
        resolve_backend,
        run_service_step,
    )

    return await run_service_step(step, resolve_backend(step, ctx.project), ctx, pool)


async def call_hook(fn: Callable[..., Any], ctx: Ctx, kwargs: Mapping[str, Any]) -> Any:
    """Invoke a hook, awaiting it when it is a coroutine function.

    Sync and async hooks are both first-class: an author writing `def collect`
    should not have to know what a coroutine is, and one writing `async def
    answer` should not be prevented from awaiting.
    """
    result = fn(ctx, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def call_step_hook(
    step: Step,
    fn: Callable[..., Any],
    ctx: Ctx,
    kwargs: Mapping[str, Any],
) -> Any:
    """Call an authored hook, applying an agent's runtime output contract."""

    if step.executor != "agent" or not step.output_schema:
        return await call_hook(fn, ctx, kwargs)
    if ctx.project is None:
        raise RuntimeError(f"agent step {step.name!r} needs a workflow project")

    from navigator_orchestrator.sdk.context import Blocked  # noqa: PLC0415
    from navigator_orchestrator.sdk.schema import validate_instance  # noqa: PLC0415
    from navigator_orchestrator.sdk.schema_sources import load_locked_schema  # noqa: PLC0415

    snapshot = load_locked_schema(ctx.project, step.output_schema)
    last_problem = "provider did not return structured output"
    for attempt in range(1, 3):
        ctx.output_schema = snapshot.schema_
        try:
            produced = await call_hook(fn, ctx, kwargs)
        except (TypeError, ValueError) as exc:
            last_problem = str(exc)
            if attempt == 1:
                continue
            raise Blocked(
                f"agent step {step.name!r} could not produce structured output after 2 attempts: "
                f"{last_problem}"
            ) from exc
        finally:
            ctx.output_schema = None

        result = validate_instance(snapshot, produced)
        ctx.detail = {
            "schema_id": result.schema_id,
            "schema_revision": result.revision,
            "valid": result.valid,
            "finding_count": len(result.findings),
            "structured_attempts": attempt,
        }
        if result.valid:
            return produced
        first = result.findings[0]
        last_problem = f"{first.path or '/'}: {first.message}"

    raise Blocked(
        f"agent step {step.name!r} returned schema-invalid output after 2 attempts: {last_problem}"
    )


def resolve_hook(
    step: Step, hooks: Mapping[str, Callable[..., Any]]
) -> tuple[Callable[..., Any], str]:
    """The implementation to run, and where it came from.

    Three sources, in precedence order:

    1. **`"file"`** — the workflow file defined a function with the step's name.
       The author's override always wins; that is what makes a template a
       starting point rather than a cage.
    2. **`"uses"`** — `Step.uses` names a registered implementation
       (`SPEC-NSP-005` §5). Resolution happens here rather than at import so
       that a template referring to a missing name is a *checkable* error and
       not an import-order accident.
    3. **`"default"`** — the callable the template carried inline.

    Which one ran is surfaced because the first question when a workflow
    misbehaves is whether the code at fault is yours or ours.
    """
    override = hooks.get(step.name)
    if override is not None:
        return override, "file"
    if step.uses:
        try:
            return resolve_uses(step.uses), "uses"
        except UnknownImplementationError as exc:
            raise StepFailed(step.name, exc) from exc
    if step.default is None:  # pragma: no cover - `check` rejects this first
        raise StepFailed(step.name, RuntimeError("required hook is not implemented"))
    return step.default, "default"


def summarise_product(value: Any, keys: Sequence[str] = ()) -> dict[str, Any]:
    """A step's `detail` for the event log — a summary, never the payload.

    Copying full drafts into every row makes the log unqueryable and duplicates
    personal data. Keys and sizes answer "what happened"; the run store holds
    the thing itself (SPEC-EDW-002 §5).

    `keys` comes from `Step.summary_keys`: the *template* names the handful of
    scalars worth having in the log, because only it knows which they are. This
    function previously carried a list of record field names, which made a
    generic summariser useful to exactly one domain (`SPEC-NSP-005` §3).
    """
    if isinstance(value, dict):
        detail: dict[str, Any] = {"keys": sorted(value)[:12]}
        for key in keys:
            found = value.get(key)
            if key not in value:
                continue
            if isinstance(found, (str, int, float, bool)) or found is None:
                detail[key] = found
            elif isinstance(found, (list, tuple, dict)):
                # A count, not the contents — a summary that can grow without
                # bound is the payload wearing a disguise.
                detail[key] = len(found)
        return detail
    if isinstance(value, (list, tuple)):
        return {"count": len(value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return {"value": str(value)[:120]}
    return {"type": type(value).__name__}
