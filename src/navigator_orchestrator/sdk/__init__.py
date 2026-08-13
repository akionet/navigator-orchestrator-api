"""`nav-service-sdk` — the client side of the platform (SPEC-NSP-002 §2).

Loads a workflow file, validates it, and executes its hooks **in the caller's
own process**. At P0/P1 it also runs the template, because there is no platform
yet; from P2 that half becomes a worker loop against `nav-service-be`.

The isolation boundary this package exists to hold: **nothing under
`navigator_orchestrator.api` may import anything from here.** The platform never loads,
binds or executes client code — `tests/test_sdk_isolation.py` asserts it.
"""

from __future__ import annotations

from navigator_orchestrator.sdk.binding import bind_kwargs, declared_parameters
from navigator_orchestrator.sdk.check import CheckError, Problem, check_file
from navigator_orchestrator.sdk.context import Blocked, Ctx, Document, FileAccess
from navigator_orchestrator.sdk.loader import LoadError, WorkflowFile, load_file
from navigator_orchestrator.sdk.runner import RunResult, StepFailed, run_template
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
from navigator_orchestrator.sdk.templates import Step, Template, TemplateRegistry

__all__ = [
    "Blocked",
    "CheckError",
    "Ctx",
    "Document",
    "FileAccess",
    "LoadError",
    "Problem",
    "RunResult",
    "SchemaContractError",
    "SchemaRef",
    "SchemaSnapshot",
    "Step",
    "StepFailed",
    "Template",
    "TemplateRegistry",
    "ValidationFinding",
    "ValidationResult",
    "WorkflowFile",
    "bind_kwargs",
    "check_file",
    "declared_parameters",
    "load_file",
    "make_schema_snapshot",
    "run_template",
    "schema_fingerprint",
    "validate_instance",
]
