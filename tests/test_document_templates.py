"""Templates defined as YAML or JSON documents.

The value is not that YAML is nicer than Python. It is that a document can be
stored, versioned, diffed and eventually edited by someone who does not deploy
code, while behaviour stays in Python where it can be tested.

So the properties worth asserting are the ones that make a document *safe to
hand to someone else*: a typo is refused rather than ignored, an implementation
that is not deployed is caught at load rather than at step seven, and the
parsed result is a `Template` indistinguishable from a hand-written one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from navigator_orchestrator.sdk.document import (
    DocumentError,
    template_from_document,
    template_from_file,
)
from navigator_orchestrator.sdk.registry import register_implementation

MINIMAL = {
    "name": "sample",
    "steps": [{"name": "only", "executor": "local", "produces": "result"}],
}


def test_a_document_becomes_an_ordinary_template() -> None:
    """Nothing downstream should be able to tell where a template came from."""
    template = template_from_document(MINIMAL)
    assert template.name == "sample"
    assert template.hook_names() == ("only",)
    assert template.published_key == "result"


def test_the_full_surface_round_trips() -> None:
    template = template_from_document(
        {
            "name": "sample",
            "doc": "A worked example.",
            "params": ["client_id", {"name": "limit", "type": "integer", "required": False}],
            "publishes": "outcome",
            "result_schema": "onboarding-outcome",
            "prompt_refs": ["a@1"],
            "steps": [
                {
                    "name": "screen",
                    "executor": "local",
                    "produces": "screening",
                    "kwargs": ["client_id"],
                    "summary_keys": ["flagged"],
                    "doc": "look it up",
                },
                {
                    "name": "review",
                    "executor": "gate",
                    "produces": "decision",
                    "when": "screening.flagged",
                    "kwargs": ["screening"],
                },
            ],
        }
    )
    assert template.param_names == ("client_id", "limit")
    assert template.input_schema()["properties"]["limit"]["type"] == "integer"
    assert template.published_key == "outcome"
    assert template.result_schema == "onboarding-outcome"
    assert template.step("review").when == "screening.flagged"


# ── typos are refused, not ignored ───────────────────────────────────────────


def test_an_unknown_step_key_is_refused() -> None:
    """A silently ignored key is a step that quietly does not do what it says."""
    document = {
        "name": "sample",
        "steps": [
            {"name": "only", "executor": "local", "produces": "result", "when_": "typo"},
        ],
    }
    with pytest.raises(DocumentError, match="unknown keys"):
        template_from_document(document)


def test_an_unknown_template_key_is_refused() -> None:
    with pytest.raises(DocumentError, match="unknown keys"):
        template_from_document({**MINIMAL, "paramz": []})


def test_a_step_missing_a_required_field_says_which() -> None:
    with pytest.raises(DocumentError, match="produces"):
        template_from_document({"name": "sample", "steps": [{"name": "x", "executor": "local"}]})


def test_a_template_needs_a_name_and_a_step() -> None:
    with pytest.raises(DocumentError, match="needs a name"):
        template_from_document({"steps": MINIMAL["steps"]})
    with pytest.raises(DocumentError, match="at least one step"):
        template_from_document({"name": "sample", "steps": []})


def test_step_validation_still_applies_and_names_the_document() -> None:
    """`Step.__post_init__` already says what is wrong; the file is added."""
    document = {
        "name": "sample",
        "steps": [{"name": "x", "executor": "local", "produces": "p", "when": "a.b"}],
    }
    with pytest.raises(DocumentError, match="when="):
        template_from_document(document, where="thing.yaml")


# ── the failure that matters most ────────────────────────────────────────────


def test_an_unregistered_uses_fails_at_load_naming_what_exists() -> None:
    """Not at step seven, by which point work has already happened.

    A document names Python that a deployed worker must already have. Catching
    that at parse time is the difference between a refused document and a run
    that gets halfway, possibly past a human decision, and then cannot continue.
    """
    document = {
        "name": "sample",
        "steps": [
            {"name": "x", "executor": "local", "produces": "p", "uses": "nobody.registered.this"}
        ],
    }
    with pytest.raises(DocumentError, match="no implementation registered"):
        template_from_document(document)


def test_a_registered_uses_is_accepted() -> None:
    register_implementation("test_doc.step", lambda ctx: "done")
    template = template_from_document(
        {
            "name": "sample",
            "steps": [{"name": "x", "executor": "local", "produces": "p", "uses": "test_doc.step"}],
        }
    )
    assert template.step("x").uses == "test_doc.step"


def test_a_gate_needs_no_implementation() -> None:
    """The engine implements gates, so a document must not be asked for one."""
    template = template_from_document(
        {
            "name": "sample",
            "steps": [{"name": "review", "executor": "gate", "produces": "decision"}],
        }
    )
    assert template.step("review").uses == ""


# ── both formats ─────────────────────────────────────────────────────────────


def test_json_and_yaml_are_the_same_document_model(tmp_path: Path) -> None:
    """Refusing JSON buys nothing.

    A workflow generated by another program is likelier to be JSON; one edited
    by a person is likelier to be YAML.
    """
    yaml_text = "name: sample\nsteps:\n  - name: only\n    executor: local\n    produces: result\n"
    (tmp_path / "a.yaml").write_text(yaml_text, encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(MINIMAL), encoding="utf-8")

    from_yaml = template_from_file(tmp_path / "a.yaml")
    from_json = template_from_file(tmp_path / "b.json")

    assert from_yaml.name == from_json.name
    assert from_yaml.hook_names() == from_json.hook_names()


def test_a_malformed_document_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("name: sample\nsteps:\n  - nonsense\n", encoding="utf-8")
    with pytest.raises(DocumentError, match=r"broken\.yaml"):
        template_from_file(path)
