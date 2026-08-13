"""B5 — the five `check` rules (SPEC-NSP-006 §7).

Every one of these is a template mistake, free to detect, and would otherwise
surface as a 404 from production at five o'clock. None of them makes a network
call, which is the property being asserted as much as the messages are.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from navigator_orchestrator.sdk.check import check_service_steps
from navigator_orchestrator.sdk.project import Backend, Project
from navigator_orchestrator.sdk.service import Call
from navigator_orchestrator.sdk.templates import Step, Template


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(
        root=tmp_path,
        backends={
            "client-service": Backend("client-service", "https://api.example.com"),
            "billing": Backend("billing", "http://localhost:8001"),
        },
    )


def one(step: Step) -> Template:
    return Template(name="t", steps=(step,))


def messages(template: Template, project: Project | None) -> str:
    return "\n".join(str(p) for p in check_service_steps(template, project))


# 1. an unknown backend name


def test_an_unknown_backend_lists_the_configured_ones(project: Project) -> None:
    template = one(
        Step("x", "service", produces="p", backend="client-servcie", call=Call("GET", "/x"))
    )
    reported = messages(template, project)
    assert "client-servcie" in reported
    assert "billing, client-service" in reported


def test_a_known_backend_passes(project: Project) -> None:
    template = one(
        Step(
            "x", "service", produces="p", backend="client-service", call=Call("GET", "/v1/records")
        )
    )
    assert check_service_steps(template, project) == []


# 2. a $name the step did not declare


def test_an_undeclared_placeholder_is_caught(project: Project) -> None:
    template = one(
        Step(
            "x",
            "service",
            produces="p",
            kwargs=("status",),
            backend="client-service",
            call=Call("GET", "/v1/record/$identifier"),
        )
    )
    reported = messages(template, project)
    assert "$identifier" in reported
    assert "status" in reported, "what is declared is named, so the fix is obvious"


# 3. a $name inside a longer string


def test_a_fragment_placeholder_is_caught(project: Project) -> None:
    template = one(
        Step(
            "x",
            "service",
            produces="p",
            kwargs=("status",),
            backend="client-service",
            call=Call("GET", "/x", query={"q": "status:$status AND live"}),
        )
    )
    assert "whole value" in messages(template, project)


# 4. a service step with no call — caught at declaration, earlier still


def test_a_service_step_with_no_call_cannot_be_constructed() -> None:
    """Earlier than `check`: the template will not import at all, which is the
    better place for it — an unimportable template cannot be half-run."""
    with pytest.raises(ValueError, match="needs call="):
        Step("x", "service", produces="p", backend="client-service")


# 5. a service step with no backend


def test_a_service_step_with_no_backend_says_what_to_add(project: Project) -> None:
    template = one(Step("x", "service", produces="p", call=Call("GET", "/x")))
    reported = messages(template, project)
    assert "does not name a backend" in reported
    assert "navigator-orchestrator.toml" in reported


# outside a project


def test_backend_names_are_not_validated_outside_a_project() -> None:
    """`check` still works outside a workflow project — it says nothing about
    backends rather than complaining about the absence, because a tutorial
    workflow that calls no API needs no manifest."""
    template = one(Step("x", "service", produces="p", backend="anything", call=Call("GET", "/x")))
    assert check_service_steps(template, None) == []


def test_a_malformed_call_is_still_caught_outside_a_project() -> None:
    """The call is the template's own business, so it is checkable anywhere."""
    template = one(Step("x", "service", produces="p", backend="b", call=Call("GET", "/x/$missing")))
    assert "$missing" in messages(template, None)


# non-service steps are untouched


def test_non_service_steps_are_ignored() -> None:
    template = Template(
        name="t",
        steps=(
            Step("a", "local", produces="x", default=lambda ctx: None),
            Step("b", "gate", produces="y"),
        ),
    )
    assert check_service_steps(template, None) == []


def test_every_problem_is_reported_not_just_the_first(project: Project) -> None:
    """Fixing one error, re-running, and finding the next is how a five-minute
    task becomes an afternoon."""
    template = Template(
        name="t",
        steps=(
            Step("a", "service", produces="p", backend="nope", call=Call("GET", "/x/$missing")),
            Step("b", "service", produces="q", call=Call("GET", "/y")),
        ),
    )
    assert len(check_service_steps(template, project)) == 3
