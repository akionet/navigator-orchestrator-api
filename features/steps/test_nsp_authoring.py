"""AC-1…AC-4, AC-10 — `features/nsp-authoring.feature`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, scenarios, then, when

from navigator_orchestrator.engine.deps import Deps
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.prompts import PromptRegistry
from navigator_orchestrator.sdk.binding import bind_kwargs
from navigator_orchestrator.sdk.check import CheckError, check_file
from navigator_orchestrator.sdk.context import Ctx, FileAccess
from navigator_orchestrator.sdk.loader import load_file
from navigator_orchestrator.sdk.runner import run_template
from navigator_orchestrator.templates import default_registry

scenarios("nsp-authoring.feature")

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "refunds.md").write_text("Refunds are accepted within 30 days.", encoding="utf-8")
    return docs


@pytest.fixture
def deps() -> Deps:
    return Deps(
        prompts=PromptRegistry.from_dir(REPO_ROOT / "prompts"),
        llm=FakeChatModel(model_name="fake:echo"),
    )


def _write(tmp_path: Path, body: str, name: str = "wf.py") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _run(path: Path, deps: Deps, corpus: Path, run_async: Any) -> Any:
    parsed, _module = load_file(path)
    template = check_file(parsed, default_registry())
    ctx = Ctx(
        params={"question": "what is the refund window?"},
        deps=deps,
        files=FileAccess(root=corpus),
    )
    return run_async(run_template(template, parsed.hooks, ctx))


# ── Given ────────────────────────────────────────────────────────────────────


@given("a workflow file containing only the WORKFLOW line")
@given("no connector is configured for the tenant")
def only_the_workflow_line(tmp_path: Path, bdd_context: dict[str, Any]) -> None:
    bdd_context["path"] = _write(tmp_path, 'WORKFLOW = "doc-qa"\n')


@given('a workflow file defining "index" before "answer"')
def index_first(tmp_path: Path, bdd_context: dict[str, Any]) -> None:
    bdd_context["first"] = _write(
        tmp_path,
        'WORKFLOW = "doc-qa"\n\n\ndef index(ctx, sources):\n    return sources\n\n\n'
        'def answer(ctx, question):\n    return f"a:{question}"\n',
        name="forwards.py",
    )


@given('a workflow file defining "answer" before "index"')
def answer_first(tmp_path: Path, bdd_context: dict[str, Any]) -> None:
    bdd_context["second"] = _write(
        tmp_path,
        'WORKFLOW = "doc-qa"\n\n\ndef answer(ctx, question):\n    return f"a:{question}"\n\n\n'
        "def index(ctx, sources):\n    return sources\n",
        name="backwards.py",
    )


@given('a workflow file whose answer hook declares only "question"')
def answer_declares_question(tmp_path: Path, bdd_context: dict[str, Any]) -> None:
    bdd_context["path"] = _write(
        tmp_path,
        'WORKFLOW = "doc-qa"\n\nSEEN = {}\n\n\ndef answer(ctx, question):\n'
        '    SEEN["kwargs"] = sorted(locals())\n    return question\n',
    )


@given('a workflow file defining a hook named "collct"')
def misspelled_hook(tmp_path: Path, bdd_context: dict[str, Any]) -> None:
    bdd_context["path"] = _write(tmp_path, 'WORKFLOW = "doc-qa"\n\n\ndef collct(ctx):\n    ...\n')


@given('a workflow file whose answer hook asks for "questoin"')
def misspelled_parameter(tmp_path: Path, bdd_context: dict[str, Any]) -> None:
    bdd_context["path"] = _write(
        tmp_path, 'WORKFLOW = "doc-qa"\n\n\ndef answer(ctx, questoin):\n    ...\n'
    )


@given('a workflow file written against a step offering only "question"')
def hook_from_before(bdd_context: dict[str, Any]) -> None:
    def answer(ctx: Any, question: str) -> str:
        return question

    bdd_context["hook"] = answer
    bdd_context["before"] = bind_kwargs(answer, available={"question": "q"}, allowed=("question",))


# ── When ─────────────────────────────────────────────────────────────────────


@when("the file is checked and run against a folder of documents")
def check_and_run(bdd_context: dict[str, Any], deps: Deps, corpus: Path, run_async: Any) -> None:
    bdd_context["result"] = _run(bdd_context["path"], deps, corpus, run_async)


@when("both files are run")
def run_both(bdd_context: dict[str, Any], deps: Deps, corpus: Path, run_async: Any) -> None:
    bdd_context["result_a"] = _run(bdd_context["first"], deps, corpus, run_async)
    bdd_context["result_b"] = _run(bdd_context["second"], deps, corpus, run_async)


@when('the step later also offers "locale"')
def step_gains_a_kwarg(bdd_context: dict[str, Any]) -> None:
    bdd_context["after"] = bind_kwargs(
        bdd_context["hook"],
        available={"question": "q", "locale": "en-GB"},
        allowed=("question", "locale"),
    )


@when("the file is checked")
def check_only(bdd_context: dict[str, Any]) -> None:
    parsed, _module = load_file(bdd_context["path"])
    try:
        check_file(parsed, default_registry())
    except CheckError as exc:
        bdd_context["error"] = exc
    else:  # pragma: no cover - the scenario asserts a failure
        pytest.fail("check should have refused this file")


# ── Then ─────────────────────────────────────────────────────────────────────


@then("an answer is produced")
def an_answer_is_produced(bdd_context: dict[str, Any]) -> None:
    assert isinstance(bdd_context["result"].output, str)
    assert bdd_context["result"].output.strip()


@then("every step reports that it used the template default")
def all_defaults(bdd_context: dict[str, Any]) -> None:
    sources = {record.source for record in bdd_context["result"].steps}
    assert sources == {"default"}


@then("both produce the same result")
def same_result(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["result_a"].output == bdd_context["result_b"].output


@then("the hook was called with only the question")
def only_question(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["result"].output == "what is the refund window?"


@then("the file still binds unchanged")
def binds_unchanged(bdd_context: dict[str, Any]) -> None:
    assert bdd_context["before"] == bdd_context["after"] == {"question": "q"}


@then('the check fails naming "collct" and suggesting "collect"')
def hook_suggestion(bdd_context: dict[str, Any]) -> None:
    message = str(bdd_context["error"])
    assert "'collct'" in message
    assert "did you mean 'collect'?" in message


@then('the check fails naming "questoin" and suggesting "question"')
def parameter_suggestion(bdd_context: dict[str, Any]) -> None:
    message = str(bdd_context["error"])
    assert "'questoin'" in message
    assert "did you mean 'question'?" in message


@then("nothing was run")
def nothing_ran(bdd_context: dict[str, Any]) -> None:
    assert "result" not in bdd_context
