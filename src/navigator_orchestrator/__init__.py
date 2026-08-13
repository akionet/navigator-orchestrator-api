"""navigator-orchestrator — a model-agnostic AI workflow engine.

Two products from one engine (`SPEC-NSP-005` §1): a **CLI** that is to workflows
what `git` is to repositories, and a **platform** that is to workflows what
GitHub Actions is to CI. The property that makes that work, and the one to
protect: the same definition runs in both places.

## This module is the supported surface

Everything a workflow project needs is exported here, and a workflow project
should import from **nowhere else**. `tests/test_public_api.py` asserts it,
because a convention nobody re-reads is not a boundary — and the boundary is
what makes `navigator-orchestrator-api` open-sourceable without dragging one company's
records along with it.

```python
from navigator_orchestrator import Ctx, Step, Template, implementation
```

Anything under `navigator_orchestrator.engine.*`, `navigator_orchestrator.sdk.*`
or `navigator_orchestrator.store.*` is
internal and may be rearranged without a major version. If something you need is
missing from here, that is a gap to fix in this file rather than a reason to
reach past it.
"""

from navigator_orchestrator.sdk.composition import build_deps
from navigator_orchestrator.sdk.context import Blocked, Ctx, Declined, Document, FileAccess
from navigator_orchestrator.sdk.graph import resume_template_graph, run_template_graph
from navigator_orchestrator.sdk.judge import Judge, JudgeError, Verdict, load_judges, run_judge
from navigator_orchestrator.sdk.preflight import Requirement
from navigator_orchestrator.sdk.project import Backend, Project, ProjectError, load_project
from navigator_orchestrator.sdk.registry import (
    UnknownImplementationError,
    implementation,
    known_implementations,
    register_implementation,
)
from navigator_orchestrator.sdk.run import (
    RunOutcome,
    arun_workflow,
    ids_from_file,
    outcomes_by_status,
    run_batch,
    run_workflow,
)
from navigator_orchestrator.sdk.runner import RunResult, StepFailed, StepRecord, run_template
from navigator_orchestrator.sdk.schema import (
    SchemaContractError,
    SchemaRef,
    SchemaSnapshot,
    ValidationFinding,
    ValidationResult,
    make_schema_snapshot,
    schema_fingerprint,
    validate_instance,
)
from navigator_orchestrator.sdk.service import Call, CallSpecError, ServiceFailed
from navigator_orchestrator.sdk.templates import (
    Executor,
    Step,
    Template,
    TemplateRegistry,
    UnknownTemplateError,
)

__all__ = [
    "Backend",
    "Blocked",
    "Call",
    "CallSpecError",
    "Ctx",
    "Declined",
    "Document",
    "Executor",
    "FileAccess",
    "Judge",
    "JudgeError",
    "Project",
    "ProjectError",
    "Requirement",
    "RunOutcome",
    "RunResult",
    "SchemaContractError",
    "SchemaRef",
    "SchemaSnapshot",
    "ServiceFailed",
    "Step",
    "StepFailed",
    "StepRecord",
    "Template",
    "TemplateRegistry",
    "UnknownImplementationError",
    "UnknownTemplateError",
    "ValidationFinding",
    "ValidationResult",
    "Verdict",
    "__version__",
    "arun_workflow",
    "build_deps",
    "ids_from_file",
    "implementation",
    "known_implementations",
    "load_judges",
    "load_project",
    "make_schema_snapshot",
    "outcomes_by_status",
    "register_implementation",
    "resume_template_graph",
    "run_batch",
    "run_judge",
    "run_template",
    "run_template_graph",
    "run_workflow",
    "schema_fingerprint",
    "validate_instance",
]

__version__ = "0.2.0"
