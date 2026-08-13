"""`navigator-orchestrator.toml` (SPEC-NSP-005 §4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from navigator_orchestrator.sdk.project import (
    ProjectError,
    find_manifest,
    load_project,
    load_project_templates,
)
from navigator_orchestrator.sdk.templates import TemplateRegistry

MANIFEST = """
[paths]
templates = "tpl"

[backends.client-service]
base_url  = "https://api.example.com"
token_env = ["SERVICE_TOKEN", "FALLBACK_TOKEN"]

[backends.billing]
base_url = "${BILLING_API_URL:-http://localhost:8001}"
timeout  = 5.0
"""


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "navigator-orchestrator.toml").write_text(MANIFEST, encoding="utf-8")
    (tmp_path / "flows" / "deep").mkdir(parents=True)
    return tmp_path


def test_the_manifest_is_found_by_walking_up(project_root: Path) -> None:
    """Which is what makes `make respond` work from anywhere inside the project
    — the difference between a tool people use and one they `cd` for."""
    found = find_manifest(project_root / "flows" / "deep")
    assert found == project_root / "navigator-orchestrator.toml"


def test_not_being_in_a_project_is_an_ordinary_answer(tmp_path: Path) -> None:
    assert find_manifest(tmp_path) is None


def test_loading_outside_a_project_says_what_it_looked_for(tmp_path: Path) -> None:
    with pytest.raises(ProjectError, match=r"navigator\-orchestrator\.toml"):
        load_project(tmp_path)


def test_declared_paths_resolve_against_the_manifest_not_the_cwd(project_root: Path) -> None:
    project = load_project(project_root / "flows" / "deep")
    assert project.paths["templates"] == (project_root / "tpl").resolve()
    assert project.paths["judges"] == (project_root / "judges").resolve(), "defaulted"


def test_a_backend_reports_which_credential_it_used_never_the_token(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The *name* is what a run records. The value must never reach a log."""
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("FALLBACK_TOKEN", "s3cret")

    backend = load_project(project_root).backend("client-service")
    assert backend.token() == ("s3cret", "FALLBACK_TOKEN")


def test_the_first_set_token_wins(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two valid credentials at once is what a migration *is*; needing a code
    change to switch between them is how migrations stall."""
    monkeypatch.setenv("SERVICE_TOKEN", "jwt")
    monkeypatch.setenv("FALLBACK_TOKEN", "legacy")

    assert load_project(project_root).backend("client-service").token() == (
        "jwt",
        "SERVICE_TOKEN",
    )


def test_no_credential_is_an_absence_not_a_crash(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("FALLBACK_TOKEN", raising=False)
    assert load_project(project_root).backend("client-service").token() is None


def test_a_base_url_falls_back_when_the_variable_is_unset(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("BILLING_API_URL", raising=False)
    assert load_project(project_root).backend("billing").base_url == "http://localhost:8001"


def test_an_unknown_backend_lists_the_configured_ones(project_root: Path) -> None:
    with pytest.raises(ProjectError, match="billing, client-service"):
        load_project(project_root).backend("client-servcie")


def test_a_backend_with_no_base_url_is_refused_by_name(tmp_path: Path) -> None:
    """Rather than producing a request to `https:///v1/records` at the step that
    needed it."""
    (tmp_path / "navigator-orchestrator.toml").write_text(
        "[backends.broken]\ntoken_env = []\n", encoding="utf-8"
    )
    with pytest.raises(ProjectError, match="'broken' has no base_url"):
        load_project(tmp_path)


def test_a_malformed_manifest_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "navigator-orchestrator.toml").write_text("[backends\n", encoding="utf-8")
    with pytest.raises(ProjectError, match=r"navigator\-orchestrator\.toml"):
        load_project(tmp_path)


def test_a_manifest_with_a_bom_loads(tmp_path: Path) -> None:
    """Windows tooling writes them routinely; a BOM must not make a project
    invisible to its own CLI."""
    (tmp_path / "navigator-orchestrator.toml").write_bytes(b"\xef\xbb\xbf[paths]\nflows = 'f'\n")
    assert load_project(tmp_path).paths["flows"].name == "f"


MANIFEST_WITH_TEMPLATES = '[paths]\ntemplates = "tpl"\n'

A_TEMPLATE = (
    "from navigator_orchestrator import Step, Template\n"
    "mine = Template(name='mine', steps=(Step('a', 'local', produces='x',"
    " default=lambda ctx: 1),))\n"
)


def with_templates(root: Path, name: str, source: str) -> Path:
    (root / "navigator-orchestrator.toml").write_text(MANIFEST_WITH_TEMPLATES, encoding="utf-8")
    (root / "tpl").mkdir(exist_ok=True)
    (root / "tpl" / name).write_text(source, encoding="utf-8")
    return root


def test_project_templates_are_discovered_and_registered(tmp_path: Path) -> None:
    """The manifest named `[paths] templates` from the first commit, but nothing
    read it — which made it a promise rather than a feature. It only showed up
    when a project first had a template of its own."""
    with_templates(tmp_path, "mine.py", A_TEMPLATE)
    registry = TemplateRegistry()

    assert load_project_templates(load_project(tmp_path), registry) == ["mine"]
    assert registry.get("mine").name == "mine"


def test_a_template_that_fails_to_import_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A template that silently does not exist sends the author hunting in the
    wrong file: `WORKFLOW = "mine"` would report "unknown workflow"."""
    with_templates(tmp_path, "broken.py", "import nonexistent_module_xyz\n")

    with pytest.raises(ProjectError, match=r"broken\.py"):
        load_project_templates(load_project(tmp_path), TemplateRegistry())


def test_a_private_module_is_not_imported(tmp_path: Path) -> None:
    """`_helpers.py` next to the templates is shared code, not a template, and
    importing it as one would register whatever it happened to define."""
    with_templates(tmp_path, "_helpers.py", "raise RuntimeError('should not be imported')\n")

    assert load_project_templates(load_project(tmp_path), TemplateRegistry()) == []


def test_loading_a_project_twice_does_not_double_register(tmp_path: Path) -> None:
    """Template modules register their `uses` implementations at import, and
    registration is an error rather than a replace. Executing the same file
    twice produced two different function objects under one name and raised
    "already registered" — so `check` then `run` in one process failed on the
    second load, as did any second test.

    Importing the same file twice being the *same module* is ordinary Python
    semantics, which is what the fix restores.
    """
    source = (
        "from navigator_orchestrator import Step, Template, implementation\n"
        "@implementation('twice.work')\n"
        "def work(ctx):\n"
        "    return 1\n"
        "mine = Template(name='twice', steps=(Step('a', 'local', produces='x',"
        " uses='twice.work'),))\n"
    )
    with_templates(tmp_path, "twice.py", source)
    project = load_project(tmp_path)

    assert load_project_templates(project, TemplateRegistry()) == ["twice"]
    assert load_project_templates(project, TemplateRegistry()) == ["twice"], "second load failed"
