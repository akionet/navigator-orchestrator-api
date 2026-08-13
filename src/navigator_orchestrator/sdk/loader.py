"""Load a workflow `.py` file into hooks (SPEC-NSP-001 §4.1).

Discovery is **by name**: every module-level function is matched against the
template's step names. Definition order is irrelevant (AC-1), because nothing
is registered and nothing runs at import — the file is data about behaviour,
read by inspection.

The file is imported, so it is executed. That is fine here and only here: the
loader runs in the *user's own* process, never on the platform
(`SPEC-NSP-002` §7). Nothing in this module may ever be imported by
`navigator_orchestrator.api`.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

__all__ = ["LoadError", "WorkflowFile", "load_file"]

#: The only mandatory line in a workflow file.
WORKFLOW_ATTR = "WORKFLOW"


class LoadError(Exception):
    """The file could not be read as a workflow definition."""


@dataclass(frozen=True, slots=True)
class WorkflowFile:
    """What a `.py` file contributes: a template name and some hooks."""

    path: Path
    workflow: str
    hooks: dict[str, Callable[..., Any]]
    #: Module-level functions that matched no step. Populated so `check` can
    #: report them; a run never sees a file with any.
    unmatched: tuple[str, ...] = ()
    #: `SKIP_JUDGES = ("sanctions@1",)` — judges this file opts out of.
    #: Deliberately ugly and deliberately explicit: turning off a compliance
    #: gate should appear in a diff as a decision (SPEC-NSP-004 §5).
    skip_judges: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.path.name


def load_file(path: str | Path) -> tuple[WorkflowFile, ModuleType]:
    """Import the file and collect its module-level functions.

    Returns the parsed result *and* the module, because a hook is a closure
    over its module globals and the module must outlive the call.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise LoadError(f"{resolved} is not a file")
    if resolved.suffix != ".py":
        raise LoadError(f"{resolved.name} is not a .py file")

    module = _import_isolated(resolved)

    workflow = getattr(module, WORKFLOW_ATTR, None)
    if workflow is None:
        raise LoadError(
            f"{resolved.name} does not set {WORKFLOW_ATTR}. "
            f'Add a line naming the template, e.g. {WORKFLOW_ATTR} = "doc-qa".'
        )
    if not isinstance(workflow, str) or not workflow.strip():
        raise LoadError(f"{resolved.name}: {WORKFLOW_ATTR} must be a non-empty string")

    hooks = {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("_")
        and (inspect.isfunction(value) or inspect.iscoroutinefunction(value))
        # Only functions *defined here*, so an imported helper is not mistaken
        # for a hook the author meant to write.
        and getattr(value, "__module__", None) == module.__name__
    }
    skip = getattr(module, "SKIP_JUDGES", ())
    if isinstance(skip, str):
        skip = (skip,)
    return (
        WorkflowFile(
            path=resolved,
            workflow=workflow.strip(),
            hooks=hooks,
            skip_judges=tuple(str(s) for s in skip),
        ),
        module,
    )


def _import_isolated(path: Path) -> ModuleType:
    """Import under a private module name so two files cannot collide."""
    module_name = f"_navigator_orchestrator_wf_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise LoadError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # the author's file: any error is theirs, not ours
        sys.modules.pop(module_name, None)
        raise LoadError(f"{path.name} raised while being imported: {exc}") from exc
    return module
