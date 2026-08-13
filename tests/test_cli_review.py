"""Read-only CLI review commands from SPEC-NSP-003 section 4."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from navigator_orchestrator.sdk import cli
from navigator_orchestrator.store.events import FileEventLog, StepEvent


def _event(
    root: Path,
    run_id: str,
    status: str,
    *,
    step: str = "run",
    detail: dict[str, Any] | None = None,
) -> None:
    FileEventLog(root).append(
        StepEvent(
            run_id=run_id,
            seq=1,
            workflow="respond",
            step=step,
            status=status,  # type: ignore[arg-type]
            detail=detail or {},
        )
    )


def test_runs_waiting_excludes_finished_and_failed_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "runs"
    monkeypatch.setenv("NAVIGATOR_RUNS_DIR", str(root))
    _event(root, "waiting", "awaiting_human")
    _event(root, "failed", "failed")
    _event(root, "done", "ok")

    assert cli.main(["runs", "--waiting"]) == 0
    output = capsys.readouterr().out
    assert "waiting" in output
    assert "failed" not in output
    assert "done" not in output


def test_show_prints_the_full_durable_gate_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "runs"
    monkeypatch.setenv("NAVIGATOR_RUNS_DIR", str(root))
    _event(
        root,
        "r1",
        "awaiting_human",
        step="review",
        detail={"doc": "approve this record", "keys": ["candidate"]},
    )
    _event(root, "r1", "awaiting_human")
    monkeypatch.setattr(
        cli,
        "_checkpoint_values",
        AsyncMock(return_value={"candidate": {"title": "Apricot Cake", "ingredients": "apricots"}}),
    )

    assert cli.main(["show", "r1"]) == 0
    output = capsys.readouterr().out
    assert "approve this record" in output
    assert '"title": "Apricot Cake"' in output
    assert '"ingredients": "apricots"' in output


def test_show_refuses_a_run_without_a_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "runs"
    monkeypatch.setenv("NAVIGATOR_RUNS_DIR", str(root))
    _event(root, "r1", "ok")

    assert cli.main(["show", "r1"]) == 2
    assert "not awaiting a human" in capsys.readouterr().err


def test_show_does_not_mislabel_a_resumed_payload_as_the_review_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "runs"
    monkeypatch.setenv("NAVIGATOR_RUNS_DIR", str(root))
    _event(root, "r1", "awaiting_human", step="review", detail={"keys": ["candidate"]})
    _event(root, "r1", "failed")

    assert cli.main(["show", "r1"]) == 2
    assert "is failed, not awaiting a human" in capsys.readouterr().err
