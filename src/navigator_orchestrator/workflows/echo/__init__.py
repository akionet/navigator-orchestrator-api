"""The `echo` reference workflow (SPEC-AIP-002 §3.9).

Its only job is to make AC-1..AC-7 assertable end-to-end with **zero business
logic**. If this file ever grows a domain rule, that rule belongs in a
capability spec instead.
"""

from navigator_orchestrator.workflows.echo.contracts import EchoInput, EchoOutput
from navigator_orchestrator.workflows.echo.workflow import EchoWorkflow

__all__ = ["EchoInput", "EchoOutput", "EchoWorkflow"]
