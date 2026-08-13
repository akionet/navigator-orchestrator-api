"""Validate a workflow file before anything runs (SPEC-NSP-001 §4.1, AC-4).

`check` is to a workflow file what boot validation is to the engine: the whole
world is verified up front so a failure is not discovered mid-run, after a
model has been paid for and a write has been half-made.

**Why unknown names are errors and never warnings.** A misspelled hook that is
silently ignored is a workflow that appears to run and quietly does nothing. It
is the same class of defect as client-service's `dependencies=[Depends(...)]`
written as a function parameter instead of a decorator argument — something
that reads correctly, passes review, and has no effect. That one cost an
unauthenticated homepage endpoint. Here it would cost an editor a day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Any

from navigator_orchestrator.sdk.binding import declared_parameters, takes_var_keyword
from navigator_orchestrator.sdk.loader import WorkflowFile
from navigator_orchestrator.sdk.templates import Template, TemplateRegistry, UnknownTemplateError

__all__ = ["CheckError", "Problem", "check_file"]


@dataclass(frozen=True, slots=True)
class Problem:
    """One thing wrong, phrased so it can be acted on without reading a spec."""

    message: str
    hint: str = ""

    def __str__(self) -> str:
        return f"{self.message}\n    {self.hint}" if self.hint else self.message


@dataclass
class CheckError(Exception):
    """One or more problems. Every problem is reported, not just the first."""

    path: Path
    problems: list[Problem] = field(default_factory=list)

    def __str__(self) -> str:
        lines = "\n".join(f"  - {problem}" for problem in self.problems)
        plural = "" if len(self.problems) == 1 else "s"
        return f"{self.path.name}: {len(self.problems)} problem{plural}\n{lines}"


def _suggest(name: str, candidates: tuple[str, ...]) -> str:
    matches = get_close_matches(name, candidates, n=1, cutoff=0.6)
    if matches:
        return f"did you mean {matches[0]!r}?"
    return f"known names: {', '.join(candidates)}" if candidates else "this step takes no values"


def check_file(parsed: WorkflowFile, registry: TemplateRegistry, project: Any = None) -> Template:
    """Validate `parsed` against its template. Raises `CheckError`, or returns.

    Four checks, in the order a reader would ask them:
      1. does `WORKFLOW` name a real template?
      2. is every function in the file a step the template has?
      3. is every required step implemented?
      4. does every hook ask only for values the step can supply?
      5. is every `service` step's call well-formed, and its backend real?
    """
    try:
        template = registry.get(parsed.workflow)
    except UnknownTemplateError as exc:
        raise CheckError(parsed.path, [Problem(str(exc))]) from exc

    problems: list[Problem] = []
    step_names = template.hook_names()

    for name in sorted(parsed.hooks):
        if name not in step_names:
            problems.append(
                Problem(
                    f"unknown hook {name!r}",
                    _suggest(name, step_names),
                )
            )

    for step in template.steps:
        if step.required and step.name not in parsed.hooks:
            problems.append(
                Problem(
                    f"missing required hook {step.name!r}",
                    step.doc or f"define `def {step.name}(ctx, ...)` in this file",
                )
            )

    for name, fn in sorted(parsed.hooks.items()):
        if name not in step_names:
            continue  # already reported; do not pile on
        step = template.step(name)
        if takes_var_keyword(fn):
            continue  # `**kw` accepts whatever the step offers
        for parameter, has_default in declared_parameters(fn):
            if parameter not in step.kwargs:
                problems.append(
                    Problem(
                        f"{name}() asks for unknown parameter {parameter!r}",
                        _suggest(parameter, step.kwargs),
                    )
                )
            elif not has_default and parameter not in step.kwargs:  # pragma: no cover
                problems.append(Problem(f"{name}() requires {parameter!r}, never supplied"))

    problems.extend(check_service_steps(template, project))
    problems.extend(check_schema_steps(template, project))

    if problems:
        raise CheckError(parsed.path, problems)
    return template


def check_schema_steps(template: Template, project: Any = None) -> list[Problem]:
    """Validate schema declarations and locks without accessing the network."""

    from navigator_orchestrator.sdk.schema import SchemaContractError  # noqa: PLC0415
    from navigator_orchestrator.sdk.schema_sources import load_locked_schema  # noqa: PLC0415

    problems: list[Problem] = []
    for step in template.steps:
        schema_name = (
            step.schema
            if step.executor == "validate"
            else step.output_schema
            if step.executor == "agent"
            else ""
        )
        if not schema_name:
            continue
        if project is None:
            problems.append(
                Problem(
                    f"schema-bound step {step.name!r} needs a navigator-orchestrator.toml project"
                )
            )
            continue
        schemas = getattr(project, "schemas", {})
        if schema_name not in schemas:
            known = ", ".join(sorted(schemas)) or "none"
            problems.append(
                Problem(
                    f"step {step.name!r} names unknown schema {schema_name!r}",
                    f"configured schemas: {known}",
                )
            )
            continue
        try:
            load_locked_schema(project, schema_name)
        except SchemaContractError as exc:
            problems.append(Problem(f"schema-bound step {step.name!r}: {exc}"))
    return problems


def check_service_steps(template: Template, project: Any = None) -> list[Problem]:
    """Validate every `service` step without touching the network (SPEC-NSP-006 §7).

    All of these are template mistakes, all are free to detect, and all would
    otherwise surface as a 404 from production at five o'clock. `project` is
    optional so `check` still works outside a workflow project — it simply
    cannot validate backend names there, and says nothing rather than
    complaining about the absence.
    """
    # Imported here rather than at module scope: `check` is used by templates
    # that never call an API, and should not pull in a transport to do it.
    from navigator_orchestrator.sdk.service import CallSpecError, validate_call  # noqa: PLC0415

    problems: list[Problem] = []
    for step in template.steps:
        if step.executor != "service":
            continue

        try:
            validate_call(step.call, step.kwargs)
        except CallSpecError as exc:
            problems.append(Problem(f"step {step.name!r}: {exc}"))

        if not step.backend:
            problems.append(
                Problem(
                    f"step {step.name!r} does not name a backend=",
                    "add backend='client-service', matching a [backends.*] in "
                    "navigator-orchestrator.toml",
                )
            )
        elif project is not None and step.backend not in getattr(project, "backends", {}):
            known = ", ".join(sorted(getattr(project, "backends", {}))) or "none"
            problems.append(
                Problem(
                    f"step {step.name!r} names unknown backend {step.backend!r}",
                    f"configured backends: {known}",
                )
            )
    return problems
