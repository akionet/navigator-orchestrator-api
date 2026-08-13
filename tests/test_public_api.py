"""The import gate (SPEC-NSP-005 §7).

The one test that decides whether the repository split stays clean. Everything
else in `SPEC-NSP-005` is a plan; this is the thing that will still be true in
six months, because it fails the build when it stops being true.

A workflow project — `navigator-workflows`, and whatever follows it — imports
from `navigator_orchestrator` and nothing deeper. Reaching into
`navigator_orchestrator.sdk.execution`
works today and turns an internal rearrangement into somebody else's outage
tomorrow. A convention nobody re-reads is not a boundary.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import navigator_orchestrator

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Directories holding workflow-project code — the consumer side of the
#: boundary. `editorial/` arrives with PLAN-EDW-R2-003 stage A and is listed
#: now so the gate is in place before the code it governs.
PROJECT_DIRS = ("editorial",)


def test_everything_exported_actually_exists() -> None:
    """`__all__` is a promise. An entry that does not resolve is a broken one,
    and it breaks on the consumer's machine rather than here."""
    for name in navigator_orchestrator.__all__:
        assert hasattr(navigator_orchestrator, name), (
            f"navigator_orchestrator.__all__ names {name!r}, which is not there"
        )


def test_the_surface_is_importable_without_touching_a_model_or_a_network() -> None:
    """Importing the public API must not require credentials.

    A package whose import needs an API key cannot be `pip install`ed and
    inspected, which is most of what open-sourcing it is for.
    """
    module = importlib.reload(navigator_orchestrator)
    assert module.Template is not None


def test_a_workflow_author_can_build_a_template_from_the_surface_alone() -> None:
    """The gate is only fair if the surface is sufficient. This is the smallest
    real template, written using nothing but `navigator_orchestrator`."""

    @navigator_orchestrator.implementation("gate_test.count")
    def count(ctx: navigator_orchestrator.Ctx, items: list[str]) -> int:
        return len(items)

    template = navigator_orchestrator.Template(
        name="gate-test",
        params=("items",),
        steps=(
            navigator_orchestrator.Step(
                "count", "local", produces="n", kwargs=("items",), uses="gate_test.count"
            ),
            navigator_orchestrator.Step("review", "gate", produces="verdict", kwargs=("n",)),
        ),
    )
    assert template.hook_names() == ("count", "review")
    assert count is navigator_orchestrator.known_implementations()["gate_test.count"]


def imports_of(path: Path) -> set[str]:
    """Every `navigator_orchestrator...` module a file imports from."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
            "navigator_orchestrator"
        ):
            found.add(node.module or "")
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.startswith("navigator_orchestrator")}
    return found


def project_files() -> list[Path]:
    return [
        path
        for directory in PROJECT_DIRS
        for path in sorted((REPO_ROOT / directory).rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


@pytest.mark.skipif(
    not any((REPO_ROOT / d).is_dir() for d in PROJECT_DIRS), reason="no project yet"
)
def test_workflow_projects_import_only_the_public_surface() -> None:
    offenders = {
        path.relative_to(REPO_ROOT).as_posix(): sorted(
            m for m in imports_of(path) if m != "navigator_orchestrator"
        )
        for path in project_files()
        if any(m != "navigator_orchestrator" for m in imports_of(path))
    }
    assert not offenders, (
        f"these files reach past the public API: {offenders}. "
        f"Import from `navigator_orchestrator` alone, or add what is missing to "
        f"`navigator_orchestrator/__init__.py` — the point of the boundary is that the "
        f"engine can be rearranged without breaking a workflow repository."
    )
