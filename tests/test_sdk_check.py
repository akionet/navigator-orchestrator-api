"""`check` catches what a silent hook would cost (SPEC-NSP-001 AC-1/AC-4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from navigator_orchestrator.sdk.check import CheckError, check_file
from navigator_orchestrator.sdk.loader import LoadError, load_file
from navigator_orchestrator.templates import default_registry


def write(tmp_path: Path, body: str, name: str = "wf.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def check(tmp_path: Path, body: str) -> None:
    parsed, _module = load_file(write(tmp_path, body))
    check_file(parsed, default_registry())


def test_the_empty_file_is_valid(tmp_path: Path) -> None:
    """AC-1 — one line is a complete workflow."""
    check(tmp_path, 'WORKFLOW = "doc-qa"\n')


def test_hook_order_does_not_matter(tmp_path: Path) -> None:
    """AC-1 — resolution is by name, so definition order is irrelevant."""
    forwards = 'WORKFLOW = "doc-qa"\ndef index(ctx, sources): ...\ndef answer(ctx, question): ...\n'
    backwards = (
        'WORKFLOW = "doc-qa"\ndef answer(ctx, question): ...\ndef index(ctx, sources): ...\n'
    )
    check(tmp_path, forwards)
    check(tmp_path, backwards)


def test_a_misspelled_hook_is_an_error_with_a_suggestion(tmp_path: Path) -> None:
    """AC-4 — the defect this whole design is most exposed to."""
    with pytest.raises(CheckError) as excinfo:
        check(tmp_path, 'WORKFLOW = "doc-qa"\ndef collct(ctx): ...\n')

    message = str(excinfo.value)
    assert "unknown hook 'collct'" in message
    assert "did you mean 'collect'?" in message


def test_a_misspelled_parameter_is_an_error_with_a_suggestion(tmp_path: Path) -> None:
    """AC-4 — the same rule one level down."""
    with pytest.raises(CheckError) as excinfo:
        check(tmp_path, 'WORKFLOW = "doc-qa"\ndef answer(ctx, questoin): ...\n')

    message = str(excinfo.value)
    assert "unknown parameter 'questoin'" in message
    assert "did you mean 'question'?" in message


def test_a_parameter_the_step_does_not_offer_lists_what_it_does(tmp_path: Path) -> None:
    with pytest.raises(CheckError) as excinfo:
        check(tmp_path, 'WORKFLOW = "doc-qa"\ndef collect(ctx, documents): ...\n')

    assert "known names: question" in str(excinfo.value)


def test_var_keyword_bypasses_parameter_checking(tmp_path: Path) -> None:
    check(tmp_path, 'WORKFLOW = "doc-qa"\ndef answer(ctx, **kw): ...\n')


def test_every_problem_is_reported_not_just_the_first(tmp_path: Path) -> None:
    with pytest.raises(CheckError) as excinfo:
        check(tmp_path, 'WORKFLOW = "doc-qa"\ndef collct(ctx): ...\ndef anser(ctx): ...\n')

    assert len(excinfo.value.problems) == 2


def test_an_unknown_workflow_names_the_registered_ones(tmp_path: Path) -> None:
    with pytest.raises(CheckError) as excinfo:
        check(tmp_path, 'WORKFLOW = "doc-quay"\n')

    assert "doc-qa" in str(excinfo.value)


def test_a_file_without_workflow_says_what_to_add(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="WORKFLOW"):
        load_file(write(tmp_path, "def collect(ctx): ...\n"))


def test_imported_helpers_are_not_mistaken_for_hooks(tmp_path: Path) -> None:
    """A `from x import y` at the top of a file must not look like a hook."""
    check(tmp_path, 'WORKFLOW = "doc-qa"\nfrom pathlib import Path\nfrom os import getcwd\n')


def test_private_functions_are_ignored(tmp_path: Path) -> None:
    check(tmp_path, 'WORKFLOW = "doc-qa"\ndef _helper(x): ...\n')


def test_a_file_that_raises_on_import_says_so(tmp_path: Path) -> None:
    with pytest.raises(LoadError, match="raised while being imported"):
        load_file(write(tmp_path, 'WORKFLOW = "doc-qa"\nraise ValueError("boom")\n'))
