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
    params: tuple[str, ...] = ()
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
