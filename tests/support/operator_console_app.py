"""Hermetic real-HTTP runtime for the operator-console delivery journey.

Started by navigator-orchestrator-app's Playwright acceptance harness. It uses the real
FastAPI routes, Runner, checkpointer and stores, but never reaches a provider.
"""

from pathlib import Path

from navigator_orchestrator.api.app import build_app
from navigator_orchestrator.config import Settings
from navigator_orchestrator.engine.checkpoint import make_memory_checkpointer
from navigator_orchestrator.engine.llm import FakeChatModel
from navigator_orchestrator.engine.workflow import WorkflowRegistry, WorkflowSource
from navigator_orchestrator.store import InMemoryRunLogStore, InMemoryRunStore
from navigator_orchestrator.workflows.approval import ApprovalWorkflow
from navigator_orchestrator.workflows.echo import EchoWorkflow

REPO_ROOT = Path(__file__).resolve().parents[2]
ECHO_YAML = """name: echo
steps:
  - id: echo
    uses: core.echo
"""
APPROVAL_YAML = """name: approval
steps:
  - id: review
    uses: core.approval
"""

saver = make_memory_checkpointer()
registry = WorkflowRegistry()
registry.register(
    EchoWorkflow(),
    source=WorkflowSource(kind="yaml", logical_name="flows/echo.yaml", text=ECHO_YAML),
)
registry.register(
    ApprovalWorkflow(checkpointer=saver),
    source=WorkflowSource(
        kind="yaml",
        logical_name="flows/approval.yaml",
        text=APPROVAL_YAML,
    ),
)

settings = Settings(
    _env_file=None,  # type: ignore[call-arg]
    model="fake:echo",
    prompts_dir=REPO_ROOT / "prompts",
    redis_url=None,
    database_url=None,
    cache_enabled=False,
)

app = build_app(
    settings,
    llm=FakeChatModel(model_name="fake:echo"),
    registry=registry,
    run_store=InMemoryRunStore(),
    run_log_store=InMemoryRunLogStore(),
    checkpointer=saver,
)
