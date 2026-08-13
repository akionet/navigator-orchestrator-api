"""Static node-purity check (SPEC-AIP-002 AC-3).

Two rules, both mechanical:

1. **No module-level clients.** A node module that builds an LLM/cache/HTTP
   client at import time has a side effect nobody injected and nobody can
   swap — which would quietly break AC-2 as well as AC-3.
2. **No in-place state mutation.** Nodes return a *partial update*; writing
   into `state` corrupts LangGraph's reducers and makes concurrent branches
   order-dependent.

Exposed as a library (not just a test) so the BDD scenario can point it at a
deliberately-bad module and assert the violation.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Violation", "check_paths", "check_source"]

#: Constructors that must never run at import time inside a workflow package.
_CLIENT_FACTORIES = frozenset(
    {
        "make_client",
        "init_chat_model",
        "FakeChatModel",
        "ChatVertexAI",
        "ChatBedrockConverse",
        "ChatAnthropic",
        "RedisCache",
        "Redis",
        "from_url",
        "PromptRegistry",
        "create_engine",
    }
)

#: Methods that mutate a mapping in place.
_MUTATING_METHODS = frozenset({"update", "setdefault", "pop", "clear", "popitem"})

#: Names conventionally bound to the graph state inside a node.
_STATE_NAMES = frozenset({"state", "s"})


@dataclass(frozen=True, slots=True)
class Violation:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.rule}] {self.detail}"


def check_source(source: str, path: str = "<memory>") -> list[Violation]:
    """Scan one module's source for purity violations."""
    tree = ast.parse(source, filename=path)
    violations: list[Violation] = []
    violations.extend(_module_level_clients(tree, path))
    violations.extend(_state_mutations(tree, path))
    return sorted(violations, key=lambda v: (v.line, v.rule))


def check_paths(paths: Iterable[Path]) -> list[Violation]:
    """Scan every `.py` file under the given files/directories."""
    violations: list[Violation] = []
    for path in paths:
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for file in files:
            violations.extend(check_source(file.read_text(encoding="utf-8"), str(file)))
    return violations


def _module_level_clients(tree: ast.Module, path: str) -> list[Violation]:
    found: list[Violation] = []
    for node in tree.body:  # module scope only — inside a function is fine
        value = _assigned_value(node)
        if value is None:
            continue
        for call in _calls(value):
            name = _called_name(call)
            if name in _CLIENT_FACTORIES:
                found.append(
                    Violation(
                        path=path,
                        line=call.lineno,
                        rule="module-level-client",
                        detail=(
                            f"{name}(...) runs at import time; nodes must receive "
                            f"collaborators via `deps`"
                        ),
                    )
                )
    return found


def _assigned_value(node: ast.stmt) -> ast.expr | None:
    """The expression a module-level statement evaluates, if it has one."""
    if isinstance(node, ast.Assign | ast.Expr):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _state_mutations(tree: ast.Module, path: str) -> list[Violation]:
    found: list[Violation] = []
    for func in _functions(tree):
        state_params = {a.arg for a in _all_args(func)} & _STATE_NAMES
        if not state_params:
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    base = _base_name(target)
                    if base in state_params and isinstance(target, ast.Subscript | ast.Attribute):
                        found.append(
                            Violation(
                                path=path,
                                line=node.lineno,
                                rule="state-mutation",
                                detail=f"assigns into `{base}`; return a partial update instead",
                            )
                        )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                base = _base_name(node.func.value)
                if base in state_params and node.func.attr in _MUTATING_METHODS:
                    found.append(
                        Violation(
                            path=path,
                            line=node.lineno,
                            rule="state-mutation",
                            detail=f"`{base}.{node.func.attr}(...)` mutates state in place",
                        )
                    )
    return found


def _functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _all_args(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    a = func.args
    return [*a.posonlyargs, *a.args, *a.kwonlyargs]


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _base_name(node: ast.expr) -> str:
    while isinstance(node, ast.Subscript | ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""
