"""`doc-qa` end to end, hermetically (SPEC-NSP-001 AC-1, AC-10).

The whole product claim at P0: one line of Python, a folder of documents, an
answer — with no platform, no store, no network and no connector.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.prompts import PromptRegistry
from navigator_orchestrator.sdk.check import check_file
from navigator_orchestrator.sdk.context import Blocked, Ctx, FileAccess
from navigator_orchestrator.sdk.loader import load_file
from navigator_orchestrator.sdk.runner import StepFailed, run_template
from navigator_orchestrator.templates import default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "refunds.md").write_text(
        "Refunds are accepted within 30 days of delivery.", encoding="utf-8"
    )
    (docs / "shipping.md").write_text("Standard shipping takes 3-5 days.", encoding="utf-8")
    (docs / "logo.png").write_bytes(b"\x89PNG not text")
    # Written by Windows tooling routinely; the BOM must not reach the model.
    (docs / "bom.md").write_text("Returns are free.", encoding="utf-8-sig")
    return docs


@pytest.fixture
def deps() -> Deps:
    return Deps(
        prompts=PromptRegistry.from_dir(REPO_ROOT / "prompts"),
        llm=FakeChatModel(model_name="fake:echo"),
    )


def build_ctx(deps: Deps, root: Path, **params: Any) -> Ctx:
    return Ctx(params=params, deps=deps, files=FileAccess(root=root))


async def run(source: str, tmp_path: Path, deps: Deps, root: Path, **params: Any) -> Any:
    path = tmp_path / "wf.py"
    path.write_text(source, encoding="utf-8")
    parsed, _module = load_file(path)
    template = check_file(parsed, default_registry())
    return await run_template(template, parsed.hooks, build_ctx(deps, root, **params))


async def test_the_empty_file_answers_a_question(tmp_path: Path, corpus: Path, deps: Deps) -> None:
    """AC-1 and AC-10 together: defaults only, and no connector anywhere."""
    result = await run(
        'WORKFLOW = "doc-qa"\n',
        tmp_path,
        deps,
        root=corpus,
        question="what is the refund window?",
    )

    assert [record.source for record in result.steps] == ["default", "default", "default"]
    # FakeChatModel echoes its prompt, so the corpus reaching the model is
    # observable in the answer — which is what "grounded" has to mean here.
    assert "30 days" in result.output
    assert "what is the refund window?" in result.output


async def test_a_hook_overrides_exactly_one_step(tmp_path: Path, corpus: Path, deps: Deps) -> None:
    source = 'WORKFLOW = "doc-qa"\n\n\ndef index(ctx, sources):\n    return sources[:1]\n'
    result = await run(source, tmp_path, deps, root=corpus, question="how long?")

    assert [record.source for record in result.steps] == ["default", "file", "default"]
    assert len(result.pool["documents"]) == 1


async def test_binary_files_are_not_collected(tmp_path: Path, corpus: Path, deps: Deps) -> None:
    result = await run('WORKFLOW = "doc-qa"\n', tmp_path, deps, root=corpus, question="q")
    names = [document.name for document in result.pool["sources"]]
    assert names == ["bom.md", "refunds.md", "shipping.md"], "logo.png is not a document"


async def test_a_byte_order_mark_never_reaches_the_model(
    tmp_path: Path, corpus: Path, deps: Deps
) -> None:
    """A BOM is invisible in an editor and a real character in the string."""
    result = await run('WORKFLOW = "doc-qa"\n', tmp_path, deps, root=corpus, question="q")
    bom = next(d for d in result.pool["sources"] if d.name == "bom.md")
    assert bom.text == "Returns are free."
    assert "﻿" not in result.output


async def test_an_async_hook_is_awaited(tmp_path: Path, corpus: Path, deps: Deps) -> None:
    source = (
        'WORKFLOW = "doc-qa"\n\n\n'
        "async def answer(ctx, question):\n"
        '    return f"async:{question}"\n'
    )
    result = await run(source, tmp_path, deps, root=corpus, question="q")
    assert result.output == "async:q"


async def test_ctx_require_blocks_the_run(tmp_path: Path, corpus: Path, deps: Deps) -> None:
    source = (
        'WORKFLOW = "doc-qa"\n\n\n'
        "def index(ctx, sources):\n"
        '    ctx.require(False, "not enough sources")\n'
        "    return sources\n"
    )
    with pytest.raises(Blocked, match="not enough sources"):
        await run(source, tmp_path, deps, root=corpus, question="q")


async def test_a_failing_hook_names_the_step(tmp_path: Path, corpus: Path, deps: Deps) -> None:
    source = 'WORKFLOW = "doc-qa"\n\n\ndef index(ctx, sources):\n    raise ValueError("bad")\n'
    with pytest.raises(StepFailed) as excinfo:
        await run(source, tmp_path, deps, root=corpus, question="q")
    assert excinfo.value.step == "index"


async def test_notes_are_collected(tmp_path: Path, corpus: Path, deps: Deps) -> None:
    result = await run('WORKFLOW = "doc-qa"\n', tmp_path, deps, root=corpus, question="q")
    assert any("indexed 3 document(s)" in note for note in result.notes)


def test_file_access_refuses_to_escape_its_root(tmp_path: Path) -> None:
    access = FileAccess(root=tmp_path / "docs")
    (tmp_path / "docs").mkdir()
    with pytest.raises(Blocked, match="outside the workflow root"):
        access.resolve("../secrets")


async def test_the_shipped_example_is_valid_and_runs(
    tmp_path: Path, corpus: Path, deps: Deps
) -> None:
    """`examples/qa.py` is executable documentation, so it is tested."""
    parsed, _module = load_file(REPO_ROOT / "examples" / "qa.py")
    template = check_file(parsed, default_registry())
    result = await run_template(
        template,
        parsed.hooks,
        build_ctx(deps, corpus, question="what is the refund window?"),
    )
    assert "30 days" in result.output
