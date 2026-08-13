"""The `approval` reference workflow (SPEC-AIP-003 §3.6).

Its only job is to make AC-1..AC-6 assertable: propose a line of text, pause
at a human gate, record what the human decided. **Zero business logic** — the
same discipline `echo` follows for R0. If this file ever grows a domain rule,
that rule belongs in a capability spec.
"""

from navigator_orchestrator.workflows.approval.contracts import ApprovalInput, ApprovalOutput
from navigator_orchestrator.workflows.approval.workflow import ApprovalWorkflow

__all__ = ["ApprovalInput", "ApprovalOutput", "ApprovalWorkflow"]
