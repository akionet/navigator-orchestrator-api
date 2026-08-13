"""Templates defined as YAML or JSON documents (`DESIGN-ARCH-001` §5).

A **parser, not a redesign.** Every key here maps to a field on `Template` or
`Step`; nothing is invented and nothing is interpreted. That is possible because
`Step.uses` and `Step.when` were made strings deliberately — a step has to stay
expressible as data for this to be a parser at all.

What this separates:

    templates/<name>.py               templates/<name>.yaml
    ───────────────────               ─────────────────────
    what each step *does*             what runs, in what order
    registered under a `uses` name    which implementation each step uses
    ordinary Python, ordinary tests   reviewable as a diff, versionable

The point is not that YAML is nicer than Python. It is that a document can be
stored, versioned, diffed and — eventually — edited by someone who does not
deploy code, while behaviour stays in Python where it can be tested.

**Loading a document does not make it safe.** A document names implementations;
resolving them still executes deployed Python. This runs in the CLI and the
worker, never in the API (`DESIGN-WRK-001` §1).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from navigator_orchestrator.sdk.registry import known_implementations
from navigator_orchestrator.sdk.templates import Param, Step, Template

__all__ = ["DocumentError", "template_from_document", "template_from_file"]

#: Every `Step` field a document may set. Anything else is a typo, and a typo
#: that is silently ignored is a step that quietly does not do what it says.
STEP_KEYS = frozenset(
    {
        "name",
        "executor",
        "produces",
        "kwargs",
        "uses",
        "summary_keys",
        "doc",
        "when",
        "command",
        "timeout",
        "backend",
        "schema",
        "input",
        "output_schema",
    }
)

TEMPLATE_KEYS = frozenset(
    {"name", "doc", "params", "steps", "prompt_refs", "requires", "publishes", "result_schema"}
)


class DocumentError(ValueError):
    """The document could not be read as a template."""


def _tuple(value: Any, field: str, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    raise DocumentError(f"{where}: {field} must be a list or a string, got {type(value).__name__}")


def _param(raw: Any, where: str) -> Param:
    """A bare string is shorthand for a required string, as it is in Python."""
    if isinstance(raw, str):
        return Param(name=raw)
    if not isinstance(raw, dict):
        raise DocumentError(f"{where}: a param must be a name or a mapping")
    unknown = set(raw) - {"name", "type", "required", "doc", "default"}
    if unknown:
        raise DocumentError(f"{where}: unknown param keys {sorted(unknown)}")
    if "name" not in raw:
        raise DocumentError(f"{where}: a param needs a name")
    return Param(
        name=str(raw["name"]),
        type=raw.get("type", "string"),
        required=bool(raw.get("required", True)),
        doc=str(raw.get("doc", "")),
        default=raw.get("default"),
    )


def _step(raw: Any, index: int, where: str) -> Step:
    if not isinstance(raw, dict):
        raise DocumentError(f"{where}: step {index} must be a mapping")
    unknown = set(raw) - STEP_KEYS
    if unknown:
        raise DocumentError(
            f"{where}: step {raw.get('name', index)!r} has unknown keys {sorted(unknown)}; "
            f"known: {sorted(STEP_KEYS)}"
        )
    for required in ("name", "executor", "produces"):
        if required not in raw:
            raise DocumentError(f"{where}: step {index} is missing {required!r}")

    try:
        return Step(
            name=str(raw["name"]),
            executor=raw["executor"],
            produces=str(raw["produces"]),
            kwargs=_tuple(raw.get("kwargs"), "kwargs", where),
            uses=str(raw.get("uses", "")),
            summary_keys=_tuple(raw.get("summary_keys"), "summary_keys", where),
            doc=str(raw.get("doc", "")),
            when=str(raw.get("when", "")),
            command=_tuple(raw.get("command"), "command", where),
            timeout=float(raw.get("timeout", 120.0)),
            backend=str(raw.get("backend", "")),
            schema=str(raw.get("schema", "")),
            input=str(raw.get("input", "")),
            output_schema=str(raw.get("output_schema", "")),
        )
    except ValueError as exc:
        # `Step.__post_init__` already says what is wrong; name the file too.
        raise DocumentError(f"{where}: {exc}") from exc


def template_from_document(document: Any, *, where: str = "<document>") -> Template:
    """Build a `Template` from a parsed YAML/JSON mapping.

    Unresolved `uses` names fail **here**, at load, naming what is registered.
    A run that reaches step seven and only then cannot resolve it is the worst
    version of this failure: work has already happened, possibly including a
    human decision.
    """
    if not isinstance(document, dict):
        raise DocumentError(f"{where}: a template document must be a mapping")
    unknown = set(document) - TEMPLATE_KEYS
    if unknown:
        raise DocumentError(
            f"{where}: unknown keys {sorted(unknown)}; known: {sorted(TEMPLATE_KEYS)}"
        )
    if "name" not in document:
        raise DocumentError(f"{where}: a template needs a name")
    raw_steps = document.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        raise DocumentError(f"{where}: a template needs at least one step")

    steps = tuple(_step(raw, index, where) for index, raw in enumerate(raw_steps))
    _check_uses(steps, where)

    try:
        return Template(
            name=str(document["name"]),
            steps=steps,
            doc=str(document.get("doc", "")),
            params=tuple(_param(p, where) for p in document.get("params") or ()),
            prompt_refs=_tuple(document.get("prompt_refs"), "prompt_refs", where),
            requires=_tuple(document.get("requires"), "requires", where),
            publishes=str(document.get("publishes", "")),
            result_schema=str(document.get("result_schema", "")),
        )
    except ValueError as exc:
        raise DocumentError(f"{where}: {exc}") from exc


def _check_uses(steps: tuple[Step, ...], where: str) -> None:
    """Every `uses` must name something registered, before anything runs."""
    known = set(known_implementations())
    missing = sorted({step.uses for step in steps if step.uses and step.uses not in known})
    if missing:
        raise DocumentError(
            f"{where}: no implementation registered for {missing}. "
            f"Registered: {sorted(known) or 'none'}. A document names Python that a "
            "deployed worker must already have — see DESIGN-ARCH-001 §4."
        )


def template_from_file(path: str | Path) -> Template:
    """Read a `.yaml`, `.yml` or `.json` template document.

    Both formats, because they are the same document model and refusing JSON
    buys nothing — a workflow generated by another program is likelier to be
    JSON, and one edited by a person is likelier to be YAML.
    """
    resolved = Path(path).expanduser()
    text = resolved.read_text(encoding="utf-8-sig")
    if resolved.suffix == ".json":
        document = json.loads(text)
    else:
        import yaml  # noqa: PLC0415 - only needed for YAML documents

        document = yaml.safe_load(text)
    return template_from_document(document, where=resolved.name)
