"""A document and the code it replaces must produce the same run.

The property the whole idea rests on. Moving orchestration out of Python and
into a versioned document is only worth doing if it changes *where the pipeline
is declared* and nothing else — a document that quietly behaves differently from
the code it replaced is worse than no document, because the diff people review
would stop predicting what runs.

Deliberately built on a throwaway project rather than the sample workflow:
`test_example_workflow_is_removable` fails the build if anything under `tests/`
learns a workflow project's name, and this is a property of the parser, not of
any one workflow.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from navigator_orchestrator import run_batch, run_workflow
from navigator_orchestrator.sdk.project import load_project, load_project_templates
from navigator_orchestrator.sdk.templates import TemplateRegistry

MANIFEST = """\
[paths]
templates = "templates"
flows     = "flows"
"""

# The Python form: behaviour as `default=`, pipeline declared in code.
CODE_TEMPLATE = """\
from typing import Any

from navigator_orchestrator.sdk.context import Ctx
from navigator_orchestrator.sdk.registry import register_implementation
from navigator_orchestrator.sdk.templates import Param, Step, Template


def _screen(ctx: Ctx, subject: str) -> dict[str, Any]:
    if subject == "missing":
        ctx.require(False, "subject has no record")
    if subject == "listed":
        ctx.decline("subject is on the list")
    return {"subject": subject, "flagged": subject == "flagged"}


def _decide(ctx: Ctx, screening: dict[str, Any], review: Any = None) -> dict[str, Any]:
    return {"subject": screening["subject"], "flagged": screening["flagged"], "review": review}


by_code = Template(
    name="by-code",
    doc="Declared in Python.",
    params=(Param("subject"),),
    publishes="outcome",
    steps=(
        Step(name="screen", executor="local", produces="screening",
             kwargs=("subject",), default=_screen),
        Step(name="review", executor="gate", produces="review",
             when="screening.flagged", kwargs=("screening",)),
        Step(name="decide", executor="local", produces="outcome",
             kwargs=("screening", "review"), default=_decide),
    ),
)

# The same functions, published for a document to name.
register_implementation("sample.screen", _screen)
register_implementation("sample.decide", _decide)
"""

# The document form: identical pipeline, wired through `uses:`.
DOC_TEMPLATE = """\
name: by-document
doc: Declared in a document.
params:
  - name: subject
    type: string
publishes: outcome
steps:
  - name: screen
    executor: local
    produces: screening
    kwargs: [subject]
    uses: sample.screen
  - name: review
    executor: gate
    produces: review
    when: screening.flagged
    kwargs: [screening]
  - name: decide
    executor: local
    produces: outcome
    kwargs: [screening, review]
    uses: sample.decide
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Module-scoped, and that is load-bearing rather than an optimisation.

    Implementation names are process-global and registration is an error rather
    than a replace — deliberately, so two workflows cannot quietly claim one
    name. A per-test project would be a *different file* registering
    `sample.screen` each time, which is exactly the collision that rule exists
    to catch. One project, loaded repeatedly, is also what actually happens.
    """
    tmp_path = tmp_path_factory.mktemp("equivalence")
    (tmp_path / "navigator-orchestrator.toml").write_text(MANIFEST, encoding="utf-8")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "by_code.py").write_text(
        textwrap.dedent(CODE_TEMPLATE), encoding="utf-8"
    )
    (tmp_path / "templates" / "by_document.yaml").write_text(DOC_TEMPLATE, encoding="utf-8")
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "code.py").write_text('WORKFLOW = "by-code"\n', encoding="utf-8")
    (tmp_path / "flows" / "doc.py").write_text('WORKFLOW = "by-document"\n', encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("ordinary", "completed"),
        ("flagged", "paused"),
        ("listed", "declined"),
        ("missing", "failed"),
    ],
)
def test_both_forms_reach_the_same_outcome(project: Path, subject: str, expected: str) -> None:
    """Every status, not just the happy path.

    A document that agreed on completions but diverged on declines would be the
    worst version of this: the disagreement would only show up on the runs
    somebody cares most about.
    """
    from_code = run_workflow("by-code", project_dir=project, subject=subject)
    from_doc = run_workflow("by-document", project_dir=project, subject=subject)

    assert from_code.status == expected
    assert from_doc.status == from_code.status
    assert from_doc.reason == from_code.reason


def test_both_forms_publish_the_same_record(project: Path) -> None:
    from_code = run_workflow("by-code", project_dir=project, subject="ordinary")
    from_doc = run_workflow("by-document", project_dir=project, subject="ordinary")

    assert from_code.output == from_doc.output


def test_both_forms_pause_at_the_same_gate(project: Path) -> None:
    """Including the conditional: `when` must survive being a document."""
    from_code = run_workflow("by-code", project_dir=project, subject="flagged")
    from_doc = run_workflow("by-document", project_dir=project, subject="flagged")

    assert from_code.gate is not None
    assert from_doc.gate is not None
    assert from_doc.gate["step"] == from_code.gate["step"] == "review"


def test_a_batch_runs_a_document_defined_workflow(project: Path) -> None:
    """Several subjects through the document form, in one process.

    Worth its own test because a document registers its implementations on
    import, and a batch loads the project once per item — so a batch is the
    path that would surface a double-registration if the module cache ever
    stopped working.
    """
    outcomes = run_batch(
        "by-document",
        [{"subject": s} for s in ("ordinary", "flagged", "listed", "ordinary")],
        project_dir=project,
    )
    assert [o.status for o in outcomes] == ["completed", "paused", "declined", "completed"]


def test_both_forms_publish_the_same_input_schema(project: Path) -> None:
    """What a console would render from either form."""
    registry = TemplateRegistry()
    load_project_templates(load_project(project), registry)

    by_code = registry.get("by-code").input_schema()
    by_doc = registry.get("by-document").input_schema()

    assert by_code["properties"] == by_doc["properties"]
    assert by_code["required"] == by_doc["required"]
