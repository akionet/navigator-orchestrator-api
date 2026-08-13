"""Built-in templates.

Templates are code, engineer-authored and in-repo. `default_registry()` is the
composition root for them, mirroring how workflows are registered in the API
app — one place that knows what exists, so a typo in `WORKFLOW` produces a list
of real names rather than a guess.
"""

from __future__ import annotations

from navigator_orchestrator.sdk.templates import TemplateRegistry
from navigator_orchestrator.templates.doc_qa import doc_qa

__all__ = ["default_registry", "doc_qa"]


def default_registry() -> TemplateRegistry:
    registry = TemplateRegistry()
    registry.register(doc_qa)
    return registry
