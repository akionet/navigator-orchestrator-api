"""`ctx` — the only route out of a workflow file (SPEC-NSP-001 §4.3).

Constructed per run from `Deps`, so hook code inherits the engine's purity
guarantee structurally rather than by trust: there is no module-level client to
reach for, and no import that yields one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import text_of

__all__ = ["Blocked", "Ctx", "Document", "FileAccess", "ModelAccess"]

#: What `FileAccess.read_dir` will treat as a document.
TEXT_SUFFIXES = (".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".csv")


class Blocked(Exception):
    """A hook refused to let the run continue. Raised by `ctx.require`.

    A *fault*: something is wrong and someone has to fix it — a missing field, an
    unresolvable reference, a precondition that should have held.
    """


class Declined(Blocked):
    """The rules said no, and that is the correct answer (`DESIGN-RUN-001` §2.3).

    A subclass so existing `except Blocked` handlers keep working, but a distinct
    type so callers that care can tell the two apart.

    The difference is not pedantry. A sanctions screen declining a client is the
    control *working*; a missing country on an address is a data error. Collapse
    them and "declined 40 clients this week" is indistinguishable from "40
    crashes" to anything watching — you alert on one and not the other.
    """


@dataclass(frozen=True, slots=True)
class Document:
    """One collected source. Deliberately flat — this is not a document model."""

    name: str
    text: str

    def __str__(self) -> str:
        return f"### {self.name}\n{self.text}"


@dataclass(frozen=True, slots=True)
class FileAccess:
    """Local filesystem access, rooted so a hook cannot wander.

    Scoped on purpose: a hook that can read arbitrary paths is a hook that
    cannot be reviewed. The root comes from `ctx.params`, and escaping it is an
    error rather than a warning.
    """

    root: Path

    def resolve(self, relative: str | Path = ".") -> Path:
        candidate = (self.root / Path(relative).expanduser()).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise Blocked(f"{candidate} is outside the workflow root {root}")
        return candidate

    def read_dir(
        self,
        relative: str | Path = ".",
        *,
        suffixes: Sequence[str] = TEXT_SUFFIXES,
    ) -> list[Document]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise Blocked(f"{directory} is not a directory")
        wanted = {s.lower() for s in suffixes}
        return [
            Document(name=path.relative_to(directory).as_posix(), text=_read(path))
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.suffix.lower() in wanted
        ]


def _read(path: Path) -> str:
    """Read a document as text.

    `utf-8-sig` rather than `utf-8`: a BOM is invisible in an editor but is a
    real character in the string, and it would be carried into the model's
    context and any citation taken from the file. Windows tooling writes them
    routinely, so this is the common case, not the exotic one.
    """
    return path.read_text(encoding="utf-8-sig", errors="replace").strip()


@dataclass(frozen=True, slots=True)
class ModelAccess:
    """Generation through the injected client. Prompts stay versioned data."""

    deps: Deps
    output_schema: dict[str, Any] | None = None

    async def draft(self, prompt_ref: str, **values: Any) -> Any:
        """Render a versioned prompt and send it to the injected model."""
        if self.deps.prompts is None:  # pragma: no cover - the CLI always injects one
            raise RuntimeError("ctx.ai needs deps.prompts; check the composition root")
        rendered = self.deps.prompts.load(prompt_ref).render(**values)
        return await self.ask(rendered)

    async def ask(self, text: str, *, temperature: float | None = None) -> Any:
        """Send raw text. Useful for a default that has no prompt of its own.

        `temperature` overrides the policy's for this call only. It exists for
        judges (`SPEC-NSP-004` §4.4): a compliance gate declaring
        `temperature: 0` needs that to reach the provider, and before this it
        did not — the field was parsed into `Judge.temperature` and then
        silently ignored, so the one control narrowing the determinism the
        spec knowingly gives up was decoration.

        `bind` is LangChain's per-call parameter override, so this works for
        every provider rather than only the one it was tested against.
        """
        client = (
            self.deps.llm if temperature is None else self.deps.llm.bind(temperature=temperature)
        )
        if self.output_schema is not None:
            structured = client.with_structured_output(self.output_schema)
            return await structured.ainvoke([HumanMessage(content=text)])
        message = await client.ainvoke([HumanMessage(content=text)])
        return text_of(message)


@dataclass(slots=True)
class Ctx:
    """Handed to every hook as the first argument."""

    params: Mapping[str, Any]
    deps: Deps
    files: FileAccess
    #: The loaded `navigator-orchestrator.toml`, when the run is inside a workflow project.
    #: Read by the engine to resolve a `service` step's backend — not by hooks,
    #: which is why it is typed loosely here rather than importing `Project` and
    #: making every `Ctx` construction depend on the manifest module.
    project: Any = None
    notes: list[str] = field(default_factory=list)
    #: Set by `ctx.skip`; read and cleared by the runner after each step.
    skipped: str = ""
    #: Engine-facing detail for the current step's event row — how the step
    #: went, as opposed to what it produced. An engine-implemented executor
    #: fills it (a `service` step puts the HTTP status, attempt count and the
    #: *name* of the credential variable here); the runner merges it into the
    #: event and clears it. Hooks have `ctx.note` and do not need this.
    detail: dict[str, Any] = field(default_factory=dict)
    #: Set only while an agent step with `output_schema` is executing.
    output_schema: dict[str, Any] | None = None

    @property
    def ai(self) -> ModelAccess:
        return ModelAccess(self.deps, self.output_schema)

    def require(self, condition: bool, message: str) -> None:
        """Assert a content rule.

        Deliberately not an `if`: an author expressing a rule should write the
        rule, not a branch. Failure blocks the run with `message`.
        """
        if not condition:
            raise Blocked(message)

    def decline(self, reason: str) -> None:
        """End the run because the rules say no — a result, not a fault.

        Use this where the workflow reached a correct negative conclusion: a
        sanctions hit, an ineligible applicant, a policy refusal. Use `require`
        where something is genuinely broken and someone must fix it.

        The distinction reaches the caller as `RunOutcome.status` — `declined`
        rather than `failed` — so a compliance team's ordinary refusals are not
        counted as crashes by whatever is watching.
        """
        raise Declined(reason)

    def skip(self, reason: str) -> None:
        """Declare that this step did no work, and why.

        Recorded as `skipped` rather than `ok`, which matters when someone later
        asks *which runs predate a capability* — the answer should be a query,
        not archaeology.

        Replaces the engine sniffing a field name to guess (`SPEC-NSP-005` §3).
        A step knows whether it skipped; the engine cannot, and should not have
        to recognise one domain's vocabulary to find out.
        """
        self.skipped = reason

    def note(self, message: str) -> None:
        """Add a line to the run's notes. Becomes a ledger entry at P5."""
        self.notes.append(message)
