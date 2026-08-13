"""`doc-qa` — ask questions of a folder of documents (SPEC-NSP-001 §7).

The deliberately small template. Three steps, one optional hook, no gates, no
connectors, and it runs offline. It is the honest test of whether the authoring
interface is light enough for a small job — if this needs more than one hook to
be useful, the interface is too heavy and the defaults are wrong.

It is also a **coupling test**: `doc-qa` configures no connector and touches no
external system, so an engine that cannot run it has absorbed the estate.
"""

from __future__ import annotations

from typing import Any, cast

from navigator_orchestrator.sdk.context import Ctx, Document
from navigator_orchestrator.sdk.templates import Step, Template

__all__ = ["PROMPT_REF", "doc_qa"]

PROMPT_REF = "doc-qa@1"


def _default_collect(ctx: Ctx) -> list[Document]:
    """Read every text file under the workflow root, recursively.

    The default that makes the empty file work (AC-1): a user who is happy
    reading a folder writes nothing at all.

    The root is `--dir`, so this reads `"."` rather than the parameter. An
    override narrowing to a subfolder writes `ctx.files.read_dir("specs")` and
    stays inside the same sandbox.
    """
    return ctx.files.read_dir(".")


def _default_index(ctx: Ctx, sources: list[Document]) -> list[Document]:
    """No-op at P0.

    Chunking and embedding belong to `SPEC-EIC-001`'s retrieval subgraph. Until
    that lands, "indexing" is passing the documents through and letting the
    context window do the work — a recorded shortcut, not a design.
    """
    ctx.note(f"indexed {len(sources)} document(s) by context-stuffing")
    return sources


async def _default_answer(ctx: Ctx, question: str, documents: list[Document]) -> str:
    """Ground an answer in the collected documents."""
    if not documents:
        raise ValueError(f"no documents to answer from; is {ctx.params.get('dir', '.')!r} right?")
    corpus = "\n\n".join(str(document) for document in documents)
    return cast(str, await ctx.ai.draft(PROMPT_REF, question=question, documents=corpus))


doc_qa = Template(
    name="doc-qa",
    doc="Answer a question from a folder of documents.",
    prompt_refs=(PROMPT_REF,),
    params=("question", "dir"),
    steps=(
        Step(
            name="collect",
            executor="local",
            produces="sources",
            kwargs=("question",),
            default=_default_collect,
            doc="return the documents to search; defaults to reading --dir",
        ),
        Step(
            name="index",
            executor="local",
            produces="documents",
            kwargs=("sources", "question"),
            default=_default_index,
            doc="narrow or reorder the sources before answering",
        ),
        Step(
            name="answer",
            executor="agent",
            produces="answer",
            kwargs=("question", "documents", "sources"),
            default=_default_answer,
            doc="produce the answer; defaults to a grounded prompt",
        ),
    ),
)


def register(registry: Any) -> None:
    registry.register(doc_qa)
