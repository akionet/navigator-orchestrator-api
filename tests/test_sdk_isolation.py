"""The platform never loads or executes client code (SPEC-NSP-002 §2, §7).

`SPEC-NSP-001` AC-5 asserts this properly once there is a worker to assert it
against. Until then the boundary is held statically, the same way R0 holds node
purity: by scanning imports rather than trusting a convention.

The rule is one-directional. The SDK may import engine contracts; the platform
may not import the SDK. If that ever inverts, arbitrary user Python becomes
reachable from a multi-tenant service, which is the one constraint
`DESIGN-NSP-001` §2 says cannot be relaxed later.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "navigator_orchestrator"
PLATFORM = ("api", "engine", "store", "workflows")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _offenders(package: str, forbidden: str) -> list[str]:
    root = SRC / package
    if not root.is_dir():
        return []
    return [
        f"{path.relative_to(SRC)} imports {name}"
        for path in sorted(root.rglob("*.py"))
        for name in _imports(path)
        if name == forbidden or name.startswith(f"{forbidden}.")
    ]


def test_the_platform_never_imports_the_sdk() -> None:
    offenders = [
        line for package in PLATFORM for line in _offenders(package, "navigator_orchestrator.sdk")
    ]
    assert offenders == [], (
        "The platform must never load, bind or execute client code:\n  " + "\n  ".join(offenders)
    )


def test_the_platform_never_imports_the_template_registry() -> None:
    """Templates carry default hook implementations, which are client-shaped."""
    offenders = [
        line
        for package in PLATFORM
        for line in _offenders(package, "navigator_orchestrator.templates")
    ]
    assert offenders == [], "\n  ".join(offenders)


def test_the_sdk_may_import_engine_contracts() -> None:
    """The permitted direction, asserted so the rule is not read as symmetric."""
    imports = {name for path in (SRC / "sdk").rglob("*.py") for name in _imports(path)}
    assert any(name.startswith("navigator_orchestrator.engine") for name in imports)
