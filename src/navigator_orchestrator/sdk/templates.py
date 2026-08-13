"""Template and step declarations (SPEC-NSP-001 §4, SPEC-NSP-002 §5).

A **template** is the engineer-authored shape of a workflow: an ordered list of
steps, each with an executor, the values it may be given, and a default
implementation. A user's `.py` file overrides individual steps by defining a
function with the step's name.

Templates are **code, not data**. Data-defined templates are `SPEC-WFB-001`
stage 3 and stay unscheduled.

At P0/P1 every step runs in-process and `executor` is recorded but not yet
dispatched on — the platform split is P2 and the `agent` executor is P4. The
field exists now so a template written today does not need rewriting then.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "ENGINE_IMPLEMENTED",
    "Executor",
    "Param",
    "ParamType",
    "Step",
    "Template",
    "TemplateRegistry",
    "UnknownTemplateError",
]

#: `SPEC-NSP-002` §5, plus `shell` (`SPEC-NSP-005` §6). Only `local`, `agent`,
#: `gate` and `shell` are reachable before P2.
Executor = Literal["local", "agent", "gate", "service", "scheduled", "shell", "validate"]

#: Executors the engine implements itself. A step using one needs no hook and
#: no `uses`, so `check` must not demand an implementation for it.
ENGINE_IMPLEMENTED: frozenset[str] = frozenset({"gate", "shell", "service", "validate"})


class UnknownTemplateError(KeyError):
    """No template is registered under that name."""


#: JSON Schema primitives a launch parameter may declare. Deliberately small:
#: these are values an operator types into a form or passes on a command line,
#: not a data model.
ParamType = Literal["string", "integer", "number", "boolean"]


@dataclass(frozen=True, slots=True)
class Param:
    """One launch input, typed (`DESIGN-WRK-001` §3.1).

    **This is an edge, not the belly.** `params` is what an operator supplies to
    start a run — the same class of thing as a workflow's Pydantic `Input`. The
    pool keys that flow *between* steps stay untyped and always will: rigid
    structure between agentic nodes is self-defeating, because the interesting
    outputs are the ones no schema anticipated.

    So: schemas at the edges, free text inside the graph. This types one edge.
    """

    name: str
    type: ParamType = "string"
    required: bool = True
    doc: str = ""
    default: Any = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a parameter needs a name")
        if self.required and self.default is not None:
            raise ValueError(
                f"parameter {self.name!r} is required but has a default; "
                "a default makes it optional, and saying both says neither"
            )


@dataclass(frozen=True, slots=True)
class Step:
    """One node of a template's DAG.

    `name` is also the **hook name** a workflow file uses to override it, which
    is what makes the mapping from file to behaviour obvious without a decorator
    or a registration call.
    """

    name: str
    executor: Executor
    #: Pool key this step's result is stored under, for later steps to consume.
    produces: str
    #: The superset of pool keys the engine may pass. A hook declares any
    #: subset by parameter name (`SPEC-NSP-001` §4.2, AC-2).
    kwargs: tuple[str, ...] = ()
    #: `None` marks the hook **required** — the file must implement it.
    default: Callable[..., Any] | None = None
    #: A registered implementation name (`SPEC-NSP-005` §5). The one field that
    #: lets a step be written as data, so a YAML template later is a parser
    #: rather than a rewrite. Overridden by a hook of the same name in the file.
    uses: str = ""
    #: Scalars from this step's product worth putting in the event log. The
    #: template names them because only the template knows which they are —
    #: the engine used to guess, and guessed in one domain's vocabulary.
    summary_keys: tuple[str, ...] = ()
    doc: str = ""
    #: `shell` steps only: the command, as an argument list. Held on the step
    #: rather than read from the pool, deliberately — see `sdk/shell.py`.
    command: tuple[str, ...] = ()
    #: `shell` steps only: seconds before the command is killed.
    timeout: float = 120.0
    #: `service` steps only: the HTTP call, as data (`SPEC-NSP-006` §2). Typed
    #: `Any` to keep this module free of an httpx import — a template
    #: declaration should not drag a transport in.
    call: Any = None
    #: `service` steps only: which backend in `navigator-orchestrator.toml` to call.
    backend: str = ""
    #: `gate` steps only: a dotted pool path deciding whether this gate is
    #: *material* for this run. Empty means always pause. `"pep.is_pep"` pauses
    #: only when that value is truthy.
    #:
    #: A string rather than a predicate, deliberately — the same reason `uses`
    #: is a name: a step must stay expressible as data, so a YAML template is a
    #: parser rather than a rewrite. It also keeps the condition reviewable in a
    #: diff instead of buried in a lambda.
    when: str = ""
    #: `validate` steps only: the configured runtime schema and pool input.
    schema: str = ""
    input: str = ""
    #: `agent` steps only: configured runtime schema passed to the provider.
    output_schema: str = ""

    def __post_init__(self) -> None:
        if self.executor == "shell" and not self.command:
            raise ValueError(
                f"shell step {self.name!r} needs command=(...); a shell step "
                f"whose command comes from anywhere but the template is remote "
                f"code execution with extra steps (SPEC-NSP-005 §6)"
            )
        if self.command and self.executor != "shell":
            raise ValueError(f"step {self.name!r} sets command= but is a {self.executor!r} step")
        if self.uses and self.default is not None:
            raise ValueError(
                f"step {self.name!r} sets both uses={self.uses!r} and default=; "
                f"pick one, or the template says two different things"
            )
        if self.executor == "service" and self.call is None:
            raise ValueError(
                f"service step {self.name!r} needs call=Call(...); without one "
                f"there is nothing to send (SPEC-NSP-006 §7)"
            )
        if (self.call is not None or self.backend) and self.executor != "service":
            raise ValueError(
                f"step {self.name!r} sets call= or backend= but is a {self.executor!r} step"
            )
        if self.executor == "validate" and (not self.schema or not self.input):
            raise ValueError(
                f"validate step {self.name!r} needs schema= and input=; validation "
                "must name both its runtime contract and candidate"
            )
        if (self.schema or self.input) and self.executor != "validate":
            raise ValueError(
                f"step {self.name!r} sets schema= or input= but is a {self.executor!r} step"
            )
        if self.when and self.executor != "gate":
            raise ValueError(
                f"step {self.name!r} sets when= but is a {self.executor!r} step; "
                "a condition that skips real work is a branch, not a gate"
            )
        if self.output_schema and self.executor != "agent":
            raise ValueError(
                f"step {self.name!r} sets output_schema= but is a {self.executor!r} step"
            )

    @property
    def required(self) -> bool:
        """Whether a workflow file *must* implement this step.

        `uses` counts as an implementation, so a step that names one is not
        required — which is the whole point of naming it.
        """
        return self.default is None and not self.uses and self.executor not in ENGINE_IMPLEMENTED


@dataclass(frozen=True, slots=True)
class Template:
    """An ordered pipeline of steps plus the prompts it may load."""

    name: str
    steps: tuple[Step, ...]
    prompt_refs: tuple[str, ...] = ()
    doc: str = ""
    #: Pool keys supplied from `ctx.params` before the first step runs.
    #:
    #: A bare string is shorthand for a required string parameter, so every
    #: template written before `Param` existed keeps working unchanged. Mixing
    #: the two is fine — typing is opt-in per parameter, not per template.
    params: tuple[str | Param, ...] = ()
    #: Environment variables the run cannot finish without, checked before the
    #: first step (SPEC-NSP-003 §5.1). Names, or `Requirement(name, why)`.
    requires: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen:
                raise ValueError(f"template {self.name!r} declares step {step.name!r} twice")
            seen.add(step.name)

    def hook_names(self) -> tuple[str, ...]:
        return tuple(step.name for step in self.steps)

    @property
    def param_specs(self) -> tuple[Param, ...]:
        """`params` normalised — bare strings become required string params."""
        return tuple(Param(name=p) if isinstance(p, str) else p for p in self.params)

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.param_specs)

    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the launch form (`DESIGN-WRK-001` §3.1).

        The **input edge**, and only that. Nothing here describes what flows
        between steps, and nothing should: an operator needs to know what to
        type, while the pool needs to stay open enough for an agent to return
        something nobody anticipated.

        A template using the bare-string shorthand publishes a thinner schema
        than one using `Param` — every field a required string. That is a real
        difference in quality, chosen per template rather than imposed.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []
        for spec in self.param_specs:
            field_schema: dict[str, Any] = {"type": spec.type}
            if spec.doc:
                field_schema["description"] = spec.doc
            if spec.default is not None:
                field_schema["default"] = spec.default
            properties[spec.name] = field_schema
            if spec.required:
                required.append(spec.name)
        schema: dict[str, Any] = {
            "type": "object",
            "title": self.name,
            "properties": properties,
        }
        if self.doc:
            schema["description"] = self.doc
        if required:
            schema["required"] = required
        return schema

    def step(self, name: str) -> Step:
        for candidate in self.steps:
            if candidate.name == name:
                return candidate
        raise KeyError(name)

    def __iter__(self) -> Iterator[Step]:
        return iter(self.steps)


@dataclass(slots=True)
class TemplateRegistry:
    """Name → template. Duplicate registration is an error, not a replace."""

    _templates: dict[str, Template] = field(default_factory=dict)

    def register(self, template: Template) -> Template:
        if template.name in self._templates:
            raise ValueError(f"template {template.name!r} is already registered")
        self._templates[template.name] = template
        return template

    def get(self, name: str) -> Template:
        try:
            return self._templates[name]
        except KeyError as exc:
            known = ", ".join(self.names()) or "none"
            raise UnknownTemplateError(f"unknown workflow {name!r} (registered: {known})") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._templates))

    def as_mapping(self) -> Mapping[str, Template]:
        return dict(self._templates)
