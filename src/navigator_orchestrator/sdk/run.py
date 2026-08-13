"""Starting a workflow from code (`DESIGN-RUN-001`).

The CLI is one caller of the engine, not the only supported one. Before this,
embedding a run meant reading `cli.py` and copying it, which is how the KYC
demo script ended up hardcoding its parameter names.

Async core, sync facade — the same shape Temporal's Python SDK uses, and the
one this codebase already had implicitly. `run_workflow` is `arun_workflow`
under `asyncio.run` and nothing else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from navigator_orchestrator.sdk.composition import build_deps
from navigator_orchestrator.sdk.context import Blocked, Ctx, Declined, FileAccess
from navigator_orchestrator.sdk.execution import StepFailed
from navigator_orchestrator.sdk.graph import run_template_graph
from navigator_orchestrator.sdk.loader import load_file
from navigator_orchestrator.sdk.project import Project, load_project, load_project_templates
from navigator_orchestrator.templates import default_registry

__all__ = ["RunOutcome", "RunStatus", "arun_workflow", "run_batch", "run_workflow"]

#: `paused` and `declined` are ordinary results, not failures. A run that stops
#: at a gate has not gone wrong, and a client the rules decline is the control
#: working. Only `failed` means something needs fixing.
RunStatus = Literal["completed", "paused", "declined", "failed"]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one run produced, whatever happened to it.

    `run_id` is populated for every status, including `failed`: a run that
    failed is still a run somebody has to look at.
    """

    status: RunStatus
    run_id: str
    workflow: str
    params: Mapping[str, Any] = field(default_factory=dict)
    #: The template's final product, when it produced one.
    output: Any = None
    #: `paused` only — what the reviewer is shown, and where.
    gate: Mapping[str, Any] | None = None
    #: `declined` and `failed` only.
    reason: str = ""

    @property
    def ok(self) -> bool:
        """Completed cleanly. Deliberately excludes `declined`.

        A decline is a correct outcome, but it is not a completion, and callers
        that treat it as one publish things they should not.
        """
        return self.status == "completed"

    @property
    def needs_human(self) -> bool:
        return self.status == "paused"


def _resolve(workflow: str, project_dir: Path | None) -> tuple[Any, Any, Project, Path]:
    """Find the project, the template and the flow file backing `workflow`."""
    root = Path(project_dir).expanduser().resolve() if project_dir else Path.cwd()
    project = load_project(root)
    registry = default_registry()
    load_project_templates(project, registry)

    flows = project.paths.get("flows") or (project.root / "flows")
    for candidate in sorted(flows.glob("*.py")):
        parsed, _module = load_file(candidate)
        if parsed.workflow == workflow:
            return parsed, registry.get(workflow), project, project.root
    known = ", ".join(sorted(p.stem for p in flows.glob("*.py"))) or "none"
    raise LookupError(
        f"no workflow file in {flows} declares WORKFLOW = {workflow!r} (files: {known})"
    )


async def arun_workflow(
    workflow: str,
    *,
    project_dir: Path | None = None,
    model: str = "",
    **params: Any,
) -> RunOutcome:
    """Run `workflow` to completion, a gate, a decline or a failure.

    A pause is a **return value**. Gates mean a run legitimately may not
    complete, so raising would put the normal case in a `try/except`.
    """
    parsed, template, project, root = _resolve(workflow, project_dir)
    deps = build_deps(model, prompts_dir=project.paths.get("prompts"))
    ctx = Ctx(
        params=dict(params),
        deps=deps,
        files=FileAccess(root=root),
        project=project,
    )

    try:
        result = await run_template_graph(template, parsed.hooks, ctx)
    except Declined as exc:
        return RunOutcome("declined", getattr(exc, "run_id", ""), workflow, params, reason=str(exc))
    except (Blocked, StepFailed) as exc:
        return RunOutcome("failed", getattr(exc, "run_id", ""), workflow, params, reason=str(exc))

    if result.gate:
        return RunOutcome("paused", result.run_id, workflow, params, gate=result.gate)
    return RunOutcome(
        "completed",
        result.run_id,
        workflow,
        params,
        output=result.pool.get(template.steps[-1].produces) if template.steps else None,
    )


def run_workflow(
    workflow: str,
    *,
    project_dir: Path | None = None,
    model: str = "",
    **params: Any,
) -> RunOutcome:
    """Blocking `arun_workflow`, for scripts, notebooks and batch jobs.

    Most callers embedding a run are not async, and making them reach for
    `asyncio.run` is friction with no payoff.
    """
    return asyncio.run(arun_workflow(workflow, project_dir=project_dir, model=model, **params))


def run_batch(
    workflow: str,
    params: Iterable[Mapping[str, Any]],
    *,
    project_dir: Path | None = None,
    model: str = "",
) -> list[RunOutcome]:
    """Run `workflow` once per parameter set, returning one outcome each.

    **One decline does not stop the rest.** A batch of five may complete three,
    pause one and decline one; a list of results cannot express that and a list
    of outcomes can. This mirrors a list of Airflow DAG-run states, and matches
    what `make batch` already does.
    """
    return [
        run_workflow(workflow, project_dir=project_dir, model=model, **dict(one)) for one in params
    ]


def ids_from_file(path: str | Path, *, param: str = "client_id") -> list[dict[str, str]]:
    """Read one id per line into parameter sets for `run_batch`.

    Blank lines and `#` comments are skipped, so a file can carry a note about
    why a particular batch exists.
    """
    lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    return [
        {param: stripped}
        for line in lines
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]


def outcomes_by_status(outcomes: Sequence[RunOutcome]) -> dict[str, list[RunOutcome]]:
    """Group a batch for reporting — completed, paused, declined, failed."""
    grouped: dict[str, list[RunOutcome]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.status, []).append(outcome)
    return grouped
