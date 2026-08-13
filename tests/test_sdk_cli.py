"""The CLI surface (P0/P1).

Exit codes matter as much as output: `check` is meant to be usable in CI and a
pre-commit hook, so "refused" and "crashed" must be distinguishable.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from navigator_orchestrator.sdk.cli import _parse_params, main

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """A directory with a corpus, made the working directory."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "refunds.md").write_text("Refunds within 30 days.", encoding="utf-8")
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(previous)


def test_check_accepts_the_shipped_examples(capsys: pytest.CaptureFixture[str]) -> None:
    for example in ("minimal.py", "qa.py"):
        assert main(["check", str(REPO_ROOT / "examples" / example)]) == 0
    assert "valid 'doc-qa' workflow" in capsys.readouterr().out


def test_check_refuses_a_typo_with_exit_2(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "wf.py").write_text(
        'WORKFLOW = "doc-qa"\ndef collct(ctx): ...\n', encoding="utf-8"
    )

    assert main(["check", "wf.py"]) == 2
    assert "did you mean 'collect'?" in capsys.readouterr().err


def test_run_resolves_dir_against_the_working_directory(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dir ./docs` means what it means in the shell that typed it."""
    (workspace / "wf.py").write_text('WORKFLOW = "doc-qa"\n', encoding="utf-8")

    assert main(["run", "wf.py", "--question", "how long?", "--dir", "docs"]) == 0
    assert "30 days" in capsys.readouterr().out


def test_run_reports_a_missing_directory_rather_than_a_traceback(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "wf.py").write_text('WORKFLOW = "doc-qa"\n', encoding="utf-8")

    assert main(["run", "wf.py", "--question", "q", "--dir", "nope"]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_unknown_flags_become_parameters() -> None:
    assert _parse_params(["--question", "why", "--dir", "./docs"]) == {
        "question": "why",
        "dir": "./docs",
    }


def test_inline_and_bare_parameter_forms() -> None:
    assert _parse_params(["--question=why", "--verbose-notes"]) == {
        "question": "why",
        "verbose_notes": True,
    }
