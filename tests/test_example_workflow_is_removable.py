"""Workflow definitions must be deletable at a single directory level.

Other teams are expected to `rm -rf workflows/<sample>` and drop their own
project in its place. That only holds if nothing in the engine, the test suite,
the BDD features or CI has quietly learned the sample's name — and "nothing
references it" is the kind of property that is true on the day it is written and
false three sprints later.

So it is asserted rather than documented. If someone adds `workflows/kyc` to a
fixture path or a CI step, this fails and names the file, at the point the
coupling is introduced rather than the day another team tries to remove it.

The companion rule lives in `test_sdk_isolation.py`: the platform may never
import the authoring SDK. Together they are the two halves of the boundary —
the engine cannot *import* definition code, and it cannot *name* a definition
either.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / "workflows"

#: Everything that must survive deleting any single workflow project.
SCANNED = ("src", "tests", "features", ".github", "conftest.py")

#: Prose may point at the sample; code and CI may not. A broken README link is a
#: docs chore, a broken import is a broken build.
TEXT_SUFFIXES = {".py", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".feature", ".mjs"}


def _projects() -> list[str]:
    if not WORKFLOWS.is_dir():
        return []
    return sorted(p.name for p in WORKFLOWS.iterdir() if p.is_dir())


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for entry in SCANNED:
        target = REPO_ROOT / entry
        if target.is_file():
            files.append(target)
        elif target.is_dir():
            files.extend(
                path
                for path in target.rglob("*")
                if path.is_file()
                and path.suffix in TEXT_SUFFIXES
                and "__pycache__" not in path.parts
                and path.name != Path(__file__).name
            )
    return files


def test_workflow_projects_are_present_to_check() -> None:
    """Guards the guard: an empty directory makes every assertion below vacuous.

    A **skip**, not a failure. Deleting the sample is a supported action — a team
    that removes it before adding their own should get a green build with an
    honest "nothing was checked", not a red one telling them off for following
    the instructions in `workflows/README.md`.
    """
    if not _projects():
        pytest.skip("workflows/ has no project directories, so there is nothing to check")


@pytest.mark.parametrize("project", _projects())
def test_a_workflow_project_carries_its_own_manifest(project: str) -> None:
    """A project is a directory with a manifest. Without one it is a folder."""
    manifest = WORKFLOWS / project / "navigator-orchestrator.toml"
    assert manifest.is_file(), (
        f"workflows/{project} has no navigator-orchestrator.toml, so it cannot be "
        "loaded or copied as a starting point"
    )


@pytest.mark.parametrize("project", _projects())
def test_no_engine_or_ci_file_names_a_workflow_project(project: str) -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)} names {project!r}"
        for path in _scanned_files()
        if project in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert offenders == [], (
        f"Deleting workflows/{project} would break these. Definitions are data the "
        "engine is handed, never something it knows the name of:\n  " + "\n  ".join(offenders)
    )


def test_the_engine_package_does_not_read_the_workflows_directory() -> None:
    """A path lookup is the coupling an import scan would miss.

    Matched narrowly on path-join syntax rather than the bare word: `workflows`
    is also the HTTP route segment in `api/routes.py`, and a URL is not a
    filesystem read.
    """
    src = REPO_ROOT / "src"
    joins = ('/ "workflows"', "/ 'workflows'", '"workflows/', "'workflows/")
    offenders = [
        f"{path.relative_to(REPO_ROOT)}"
        for path in src.rglob("*.py")
        if "__pycache__" not in path.parts
        and any(j in path.read_text(encoding="utf-8", errors="ignore") for j in joins)
    ]
    assert offenders == [], (
        "The engine resolves a workflow project from the manifest it is given, "
        "never from a hard-coded directory:\n  " + "\n  ".join(offenders)
    )
