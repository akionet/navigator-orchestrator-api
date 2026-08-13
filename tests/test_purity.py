"""Node purity, enforced (SPEC-AIP-002 AC-3, TODO-7).

The check runs against the real `workflows/` tree, so it is a standing gate on
every capability added after R0 — not just a demo on a synthetic module.
"""

from __future__ import annotations

from pathlib import Path

from navigator_orchestrator.engine.purity import check_paths, check_source

WORKFLOWS = Path(__file__).resolve().parents[1] / "src" / "navigator_orchestrator" / "workflows"


def test_shipped_workflows_are_pure() -> None:
    violations = check_paths([WORKFLOWS])
    assert violations == [], "\n".join(str(v) for v in violations)


def test_module_level_client_is_a_violation() -> None:
    source = """
from navigator_orchestrator.engine.llm import make_client
CLIENT = make_client(policy)
"""
    rules = {v.rule for v in check_source(source)}
    assert rules == {"module-level-client"}


def test_annotated_module_level_client_is_a_violation() -> None:
    source = "CLIENT: object = RedisCache('redis://x')\n"
    assert [v.rule for v in check_source(source)] == ["module-level-client"]


def test_client_built_inside_a_function_is_fine() -> None:
    source = """
def build(policy):
    return make_client(policy)
"""
    assert check_source(source) == []


def test_subscript_state_assignment_is_a_violation() -> None:
    source = """
async def node(state, deps):
    state["scratch"]["x"] = 1
    return {}
"""
    assert [v.rule for v in check_source(source)] == ["state-mutation"]


def test_attribute_state_assignment_is_a_violation() -> None:
    source = """
async def node(state, deps):
    state.scratch = {}
    return {}
"""
    assert [v.rule for v in check_source(source)] == ["state-mutation"]


def test_state_update_call_is_a_violation() -> None:
    source = """
async def node(state, deps):
    state.update({"a": 1})
    return {}
"""
    assert [v.rule for v in check_source(source)] == ["state-mutation"]


def test_returning_a_partial_update_is_pure() -> None:
    source = """
async def node(state, deps):
    scratch = state.get("scratch") or {}
    return {"scratch": {**scratch, "x": 1}}
"""
    assert check_source(source) == []


def test_mutating_a_local_that_is_not_state_is_fine() -> None:
    source = """
async def node(state, deps):
    local = dict(state.get("scratch") or {})
    local["x"] = 1
    return {"scratch": local}
"""
    assert check_source(source) == []


def test_violation_renders_path_and_line() -> None:
    violations = check_source("CLIENT = make_client(p)\n", "nodes.py")
    assert str(violations[0]).startswith("nodes.py:1 [module-level-client]")


def test_check_paths_walks_directories(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "bad.py").write_text("C = make_client(p)\n", encoding="utf-8")
    assert len(check_paths([tmp_path])) == 1
