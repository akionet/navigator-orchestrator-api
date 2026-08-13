"""Versioned prompt registry + boot validation (SPEC-AIP-002 §3.4, TODO-3, AC-4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from navigator_orchestrator.engine.prompts import (
    MissingPromptError,
    PromptError,
    PromptRegistry,
    PromptRenderError,
    parse_ref,
)


def write(root: Path, ref: str, body: str) -> Path:
    prompt_id, version = ref.split("@")
    path = root / prompt_id / f"{version}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_parse_ref() -> None:
    assert parse_ref("echo@1") == ("echo", 1)


@pytest.mark.parametrize("ref", ["echo", "echo@", "@1", "Echo@1", "echo@one"])
def test_malformed_refs_are_rejected(ref: str) -> None:
    with pytest.raises(PromptError):
        parse_ref(ref)


def test_loads_and_renders_the_shipped_prompt(prompts: PromptRegistry) -> None:
    prompt = prompts.load("echo@1")
    assert prompt.ref == "echo@1"
    assert prompt.inputs == ("text",)
    assert prompt.render(text="ping") == "ping"


def test_render_requires_declared_inputs(prompts: PromptRegistry) -> None:
    with pytest.raises(PromptRenderError, match="text"):
        prompts.load("echo@1").render()


def test_missing_prompt_lists_what_is_available(prompts: PromptRegistry) -> None:
    with pytest.raises(MissingPromptError, match="echo@1"):
        prompts.load("echo@2")


def test_validate_all_passes_for_registered_refs(prompts: PromptRegistry) -> None:
    prompts.validate_all(["echo@1"])


def test_validate_all_fails_fast_on_a_missing_prompt(prompts: PromptRegistry) -> None:
    """AC-4: this is what takes the app down at boot rather than mid-run."""
    with pytest.raises(MissingPromptError, match="echo@2"):
        prompts.validate_all(["echo@1", "echo@2"])


def test_validate_all_catches_a_renamed_placeholder(tmp_path: Path) -> None:
    write(tmp_path, "drift@1", "---\nid: drift\nversion: 1\ninputs:\n  - text\n---\n{txet}\n")
    registry = PromptRegistry.from_dir(tmp_path)
    with pytest.raises(MissingPromptError) as excinfo:
        registry.validate_all(["drift@1"])
    assert "undeclared" in str(excinfo.value)
    assert "unused" in str(excinfo.value)


def test_front_matter_is_required(tmp_path: Path) -> None:
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "1.md").write_text("just text", encoding="utf-8")
    with pytest.raises(PromptError, match="front-matter"):
        PromptRegistry.from_dir(tmp_path)


def test_declared_id_must_match_its_path(tmp_path: Path) -> None:
    write(tmp_path, "wrong@1", "---\nid: right\nversion: 1\ninputs: []\n---\nbody\n")
    with pytest.raises(PromptError, match="declares"):
        PromptRegistry.from_dir(tmp_path)


def test_missing_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(MissingPromptError, match="does not exist"):
        PromptRegistry.from_dir(tmp_path / "nope")


def test_refs_are_sorted(tmp_path: Path) -> None:
    write(tmp_path, "b@1", "---\nid: b\nversion: 1\ninputs: []\n---\nb\n")
    write(tmp_path, "a@2", "---\nid: a\nversion: 2\ninputs: []\n---\na\n")
    assert PromptRegistry.from_dir(tmp_path).refs() == ("a@2", "b@1")
