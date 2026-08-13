"""LLM decision gates — single-hop judges declared in YAML (SPEC-NSP-004).

> *"ideally this could be done in a low code way, just adding the system prompt
> and how it fits into the workflow in an abstracted manner eg yaml file"*

Adding a compliance decision to a workflow is **adding a file**. No Python, no
new node, no template edit.

```yaml
id: sanctions
version: 1
applies_to: [respond, approve]
before: publish
inputs: [candidate.ingredients]
on_fail: block
prompt: |
  You decide whether a record is entirely plant-based...
```

## Three rules that carry the weight

**1. Failure is not a pass** (§4.3). The model errors, the output will not
parse, or there is no credential — all three block. A judge that silently
approves whenever it is broken is worse than no judge, because it *looks* like
a control. Everything in `run_judge` funnels to that.

**2. It sees only its declared inputs** (§7 Q2). A judge shown the whole pool is
a judge whose prompt drifts as the template grows. It also matters concretely
here: `sourceDocuments` is the conventional record a the downstream product dish was adapted
from, and it is *expected* to be non-sanctions. A judge shown the source would fail
exactly the records the process exists to produce.

**3. Single-hop.** One call, no tools, no loop. Anything needing more is a
workflow step, and should be written as one.

## Why not a deterministic validator

Reversed on 2026-08-08 by the person who knows how these records are made. A
substring match cannot tell `350g ground beef` from `plant-based beef mince`,
and the allow-list that patches it — `butternut`, `peanut butter`, `cocoa
butter`, `eggplant` — is never finished. Deciding whether an ingredient is
animal-derived is a semantic judgement, so it goes to something that can make
semantic judgements.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from navigator_orchestrator.sdk.context import Blocked, Ctx

__all__ = [
    "Judge",
    "JudgeError",
    "Verdict",
    "build_prompt",
    "judges_for",
    "load_judges",
    "parse_judge",
    "parse_verdict",
    "render_inputs",
    "run_judge",
]

OnFail = Literal["block", "warn"]

#: How many times a judge is asked again when its output will not parse. Once:
#: a model that cannot produce JSON twice will not on the third attempt (§7 Q4).
PARSE_RETRIES = 1

#: The first fenced or bare JSON object in a reply.
JSON_BLOB = re.compile(r"\{.*\}", re.DOTALL)


class JudgeError(RuntimeError):
    """A judge is declared wrongly. Raised at load or `check`, never mid-run."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """What a judge decided."""

    verdict: Literal["pass", "fail"]
    reason: str = ""
    offending: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def summary(self) -> dict[str, Any]:
        """For the event log. A summary, never the payload."""
        detail: dict[str, Any] = {"verdict": self.verdict}
        if self.reason:
            detail["reason"] = self.reason[:300]
        if self.offending:
            detail["offending"] = list(self.offending[:10])
        return detail


@dataclass(frozen=True, slots=True)
class Judge:
    """One declared decision gate."""

    id: str
    version: int
    prompt: str
    applies_to: tuple[str, ...]
    inputs: tuple[str, ...]
    #: `before` or `after` named steps. Exactly one of the two is set.
    #:
    #: A **tuple**, because one judge file has to cover flows that name
    #: their write step differently: `respond` ends in `store` and `approve`
    #: ends in `publish`, and duplicating the prompt to say so would make
    #: the two drift. The judge attaches before every named step the
    #: template actually has, and at least one must exist.
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    on_fail: OnFail = "block"
    temperature: float = 0.0
    title: str = ""
    source: Path | None = None

    @property
    def ref(self) -> str:
        """`sanctions@1` — what appears in the event log and in `SKIP_JUDGES`."""
        return f"{self.id}@{self.version}"

    @property
    def anchors(self) -> tuple[str, ...]:
        return self.before or self.after

    @property
    def node_name(self) -> str:
        """Its name as a graph node.

        Prefixed rather than bare, because `add_node` on an existing name
        *replaces* it — a judge called `publish` would silently delete the
        publish step and the run would look like it worked. `build_graph`
        asserts the absence of a collision rather than trusting the prefix.

        A hyphen, not a colon: LangGraph reserves `:` in node names, which it
        told us by refusing at build time. Better there than at run time.
        """
        return f"judge-{self.id}"

    def describe(self) -> str:
        where = "before" if self.before else "after"
        return f"{self.ref}  {where} {', '.join(self.anchors)}; on_fail={self.on_fail}"


def load_judges(directory: Path) -> list[Judge]:
    """Every judge under `judges/<id>/<version>.yaml`.

    Validated **at load**, the same posture as the prompt registry: a malformed
    judge should stop the run before it starts, not at the step it guards. A
    compliance gate that fails to load and is therefore skipped is the exact
    failure mode §4.3 exists to prevent.
    """
    if not directory.is_dir():
        return []

    judges: list[Judge] = []
    for path in sorted(directory.rglob("*.y*ml")):
        judges.append(parse_judge(path))

    seen: dict[str, Path] = {}
    for judge in judges:
        if judge.ref in seen:
            raise JudgeError(
                f"judge {judge.ref} is declared twice: {seen[judge.ref]} and {judge.source}"
            )
        seen[judge.ref] = judge.source or path
    return judges


def parse_judge(path: Path) -> Judge:
    """One YAML file to a `Judge`. Every error names the file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise JudgeError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise JudgeError(f"{path}: expected a mapping, got {type(raw).__name__}")

    def required(key: str) -> Any:
        if key not in raw or raw[key] in (None, ""):
            raise JudgeError(f"{path}: missing required key {key!r}")
        return raw[key]

    before = _as_tuple(raw.get("before"))
    after = _as_tuple(raw.get("after"))
    if bool(before) == bool(after):
        raise JudgeError(
            f"{path}: set exactly one of before: or after: — a judge with "
            f"neither has nowhere to run, and one with both has two"
        )

    on_fail = str(raw.get("on_fail", "block"))
    if on_fail not in ("block", "warn"):
        raise JudgeError(f"{path}: on_fail must be 'block' or 'warn', got {on_fail!r}")

    applies_to = raw.get("applies_to")
    if isinstance(applies_to, str):
        applies_to = [applies_to]
    if not applies_to:
        raise JudgeError(f"{path}: missing required key 'applies_to'")

    inputs = raw.get("inputs")
    if isinstance(inputs, str):
        inputs = [inputs]
    if not inputs:
        # Not a default. A judge shown nothing would confidently judge nothing,
        # and pass — which is the silent-approval failure in another costume.
        raise JudgeError(f"{path}: missing required key 'inputs'; a judge must be shown something")

    return Judge(
        id=str(required("id")),
        version=int(required("version")),
        prompt=str(required("prompt")),
        applies_to=tuple(str(v) for v in applies_to),
        inputs=tuple(str(v) for v in inputs),
        before=before,
        after=after,
        on_fail=on_fail,  # type: ignore[arg-type]
        temperature=float(raw.get("temperature", 0.0)),
        title=str(raw.get("title", "")),
        source=path,
    )


def _as_tuple(value: Any) -> tuple[str, ...]:
    """A string or a list, both accepted. One step is the common case and
    should not have to be written as a list of one."""
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def judges_for(judges: list[Judge], template_name: str, skip: tuple[str, ...] = ()) -> list[Judge]:
    """Those attached to this template, minus any the workflow file disabled."""
    return [j for j in judges if template_name in j.applies_to and j.ref not in skip]


def render_inputs(judge: Judge, pool: dict[str, Any]) -> str:
    """The evidence, and only the evidence.

    `candidate.ingredients` walks the pool by dots. A missing input is an
    error rather than an omission: a judge silently shown less than it was
    declared to see is a judge whose verdict means less than it appears to.
    """
    blocks: list[str] = []
    for ref in judge.inputs:
        value: Any = pool
        for part in ref.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                raise JudgeError(
                    f"judge {judge.ref} declares input {ref!r}, which the run has "
                    f"not produced; available: {', '.join(sorted(pool)) or 'nothing'}"
                )
        rendered = value if isinstance(value, str) else json.dumps(value, indent=2, default=str)
        blocks.append(f"### {ref}\n{rendered}")
    return "\n\n".join(blocks)


def parse_verdict(reply: str) -> Verdict:
    """The model's reply to a typed verdict.

    Tolerant about wrapping — a fenced block or a sentence of preamble is
    normal — and strict about content. Anything it cannot read raises, and the
    caller treats that as a failure to decide rather than as approval.
    """
    match = JSON_BLOB.search(reply or "")
    if match is None:
        raise JudgeError(f"no JSON object in the reply: {(reply or '').strip()[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise JudgeError(f"reply is not valid JSON ({exc}): {match.group(0)[:200]!r}") from exc
    if not isinstance(data, dict):
        raise JudgeError(f"expected a JSON object, got {type(data).__name__}")

    raw = str(data.get("verdict", "")).strip().lower()
    if raw not in ("pass", "fail"):
        # Not defaulted to fail *silently*: the caller blocks on this exception
        # anyway, and saying what came back is what makes a bad prompt fixable.
        raise JudgeError(f"verdict must be 'pass' or 'fail', got {data.get('verdict')!r}")

    offending = data.get("offending") or []
    if isinstance(offending, str):
        offending = [offending]
    return Verdict(
        verdict=raw,  # type: ignore[arg-type]
        reason=str(data.get("reason", "")).strip(),
        offending=tuple(str(o) for o in offending),
    )


def build_prompt(judge: Judge, pool: dict[str, Any]) -> str:
    """System prompt, evidence, and the output contract.

    The contract is appended by the engine rather than written in every YAML
    file: it is the engine that has to parse the answer, so it is the engine's
    business to ask for the shape it can read.
    """
    return (
        f"{judge.prompt.rstrip()}\n\n"
        f"Here is what you are judging:\n\n{render_inputs(judge, pool)}\n\n"
        f"Reply with JSON and nothing else, in exactly this shape:\n"
        f'{{"verdict": "pass" | "fail", "reason": "<one sentence>", '
        f'"offending": ["<ingredient>", ...]}}\n'
    )


async def run_judge(judge: Judge, ctx: Ctx, pool: dict[str, Any]) -> Verdict:
    """Ask the judge, and hold it to §4.3.

    Every route that does not end in a readable verdict ends in `Blocked` when
    `on_fail: block`. There is deliberately no path here that returns a passing
    verdict because something went wrong.
    """
    prompt = build_prompt(judge, pool)
    verdict: Verdict | None = None
    failure: Exception | None = None

    for attempt in range(PARSE_RETRIES + 1):
        try:
            verdict = parse_verdict(await ctx.ai.ask(prompt, temperature=judge.temperature))
            break
        except JudgeError as exc:
            # Unparseable. Worth one more ask: a model does occasionally wrap
            # its JSON in an apology. Twice is a prompt problem, not a fluke.
            failure = exc
            ctx.note(f"judge {judge.ref}: unreadable verdict (attempt {attempt + 1}) - {exc}")
        except Exception as exc:
            # The model errored, or there is no credential. Not retried: a
            # second identical call reproduces both.
            failure = exc
            break

    if verdict is None:
        return _no_verdict(judge, ctx, failure or RuntimeError("no verdict"))

    ctx.detail.update(judge=judge.ref, verdict=verdict.verdict, on_fail=judge.on_fail)
    if verdict.passed:
        ctx.note(f"judge {judge.ref}: pass")
        return verdict

    detail = verdict.reason or "no reason given"
    if verdict.offending:
        detail = f"{detail} ({', '.join(verdict.offending)})"
    if judge.on_fail == "warn":
        ctx.note(f"judge {judge.ref}: FAIL (warn only) - {detail}")
        return verdict
    raise Blocked(f"judge {judge.ref} refused this: {detail}")


def _no_verdict(judge: Judge, ctx: Ctx, cause: Exception) -> Verdict:
    """No verdict was produced. §4.3: that is not a pass.

    The alternative — approving because the judge is broken — is worse than
    having no judge, since it looks like a control and reports like one.
    """
    ctx.detail.update(
        judge=judge.ref, verdict="none", on_fail=judge.on_fail, error=type(cause).__name__
    )
    if judge.on_fail == "warn":
        ctx.note(f"judge {judge.ref}: no verdict ({cause}) - continuing, on_fail=warn")
        return Verdict(verdict="fail", reason=f"no verdict: {cause}")
    raise Blocked(f"judge {judge.ref} could not reach a verdict, so nothing is approved: {cause}")
