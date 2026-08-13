"""Typed launch parameters, and the line they must not cross.

`params` is the workflow's **input edge** — what an operator types to start a
run. Typing it lets the console render a form instead of a free-text box.

The pool keys that flow *between* steps are a different thing entirely and stay
untyped. Rigid structure between agentic nodes is self-defeating: the useful
outputs are frequently the ones no schema anticipated. Schemas at the edges,
free text inside the graph — and `test_the_graph_itself_stays_untyped` is what
stops that being a slogan.
"""

from __future__ import annotations

import pytest

from navigator_orchestrator.sdk.templates import Param, Step, Template


def _template(*params: str | Param, doc: str = "") -> Template:
    return Template(
        name="sample",
        doc=doc,
        params=params,
        steps=(Step(name="only", executor="local", produces="result"),),
    )


# ── the shorthand keeps working ──────────────────────────────────────────────


def test_a_bare_string_is_a_required_string_parameter() -> None:
    """Every template written before `Param` existed keeps working unchanged."""
    spec = _template("client_id").param_specs[0]
    assert spec == Param(name="client_id", type="string", required=True)


def test_names_survive_either_form() -> None:
    template = _template("client_id", Param("limit", type="integer", required=False, default=50))
    assert template.param_names == ("client_id", "limit")


def test_the_two_forms_mix() -> None:
    """Typing is opt-in per parameter, not per template."""
    schema = _template("client_id", Param("dry_run", type="boolean", required=False)).input_schema()
    assert schema["properties"]["client_id"]["type"] == "string"
    assert schema["properties"]["dry_run"]["type"] == "boolean"
    assert schema["required"] == ["client_id"]


# ── the derived schema ───────────────────────────────────────────────────────


def test_the_schema_describes_the_launch_form() -> None:
    schema = _template(
        Param("client_id", doc="Client to screen"),
        Param("limit", type="integer", required=False, default=50),
        doc="Screen a client.",
    ).input_schema()

    assert schema["type"] == "object"
    assert schema["title"] == "sample"
    assert schema["description"] == "Screen a client."
    assert schema["properties"]["client_id"]["description"] == "Client to screen"
    assert schema["properties"]["limit"] == {"type": "integer", "default": 50}
    assert schema["required"] == ["client_id"], "an optional parameter is not required"


def test_a_template_with_no_parameters_still_produces_a_schema() -> None:
    schema = _template().input_schema()
    assert schema["properties"] == {}
    assert "required" not in schema, "an empty required list is noise, not information"


def test_required_and_default_together_are_refused() -> None:
    """Saying both says neither."""
    with pytest.raises(ValueError, match="required but has a default"):
        Param("client_id", required=True, default="CL-0001")


def test_a_parameter_needs_a_name() -> None:
    with pytest.raises(ValueError, match="needs a name"):
        Param("   ")


# ── the line ─────────────────────────────────────────────────────────────────


def test_the_graph_itself_stays_untyped() -> None:
    """The guard on the principle, not on an implementation detail.

    A `Step` says what it *produces* by name and nothing about the shape of it.
    If a `schema`-like field ever appears on the general step contract, an agent
    can no longer hand the next node something unanticipated — which is the
    behaviour this engine exists to support.

    `Step.schema` and `Step.output_schema` are deliberately excluded: both are
    edges too. `validate` steps check a runtime contract on the way out, and
    `output_schema` shapes what a provider returns. Neither constrains what one
    node may hand another.
    """
    step = Step(name="draft", executor="agent", produces="candidate")

    assert step.produces == "candidate"
    assert not hasattr(step, "produces_schema")
    assert not hasattr(step, "pool_schema")
    assert not hasattr(step, "types")


# ── the output edge ──────────────────────────────────────────────────────────


def test_the_published_key_defaults_to_the_last_step() -> None:
    """Right often enough to be the default."""
    assert _template().published_key == "result"


def test_a_template_may_name_what_it_publishes() -> None:
    """Wrong often enough to be worth naming.

    A template ending in a step that *records* something rather than producing
    the answer would otherwise publish the wrong key.
    """
    template = Template(
        name="sample",
        publishes="answer",
        steps=(
            Step(name="think", executor="agent", produces="answer"),
            Step(name="log_it", executor="local", produces="written"),
        ),
    )
    assert template.published_key == "answer"


def test_a_template_with_no_steps_publishes_nothing() -> None:
    assert Template(name="empty", steps=()).published_key == ""


def test_the_result_schema_is_a_ref_not_an_inline_schema() -> None:
    """The manifest already owns runtime contracts.

    A second place to put one is a second place to forget to update.
    """
    template = Template(
        name="sample",
        steps=(Step(name="only", executor="local", produces="result"),),
        result_schema="onboarding-outcome",
    )
    assert template.result_schema == "onboarding-outcome"
    assert isinstance(template.result_schema, str)


def test_declaring_an_output_shape_is_optional() -> None:
    """A workflow publishing free-form output stays valid.

    Requiring a result schema would push structure onto workflows whose answer
    genuinely is prose, which is the belly problem wearing an edge's clothes.
    """
    assert _template().result_schema == ""


def test_params_are_the_only_typed_surface_on_a_template() -> None:
    template = _template(Param("client_id"))
    assert hasattr(template, "input_schema")
    assert not hasattr(template, "pool_schema"), (
        "typing the pool would put a schema between agentic nodes, which is the "
        "thing this design deliberately does not do"
    )
