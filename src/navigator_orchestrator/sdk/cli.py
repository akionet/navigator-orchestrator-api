"""`navigator-orchestrator` — check and run a workflow file (P0/P1).

    navigator-orchestrator check qa.py
    navigator-orchestrator run   qa.py --question "what is our refund window?" --dir ./docs

Any `--flag value` the parser does not recognise becomes a run parameter, so a
template can grow inputs without the CLI growing flags. That is the same
superset-binding idea as `SPEC-NSP-001` §4.2, applied one layer out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from navigator_orchestrator.engine.checkpoint import checkpointer_scope
from navigator_orchestrator.sdk.check import CheckError, check_file
from navigator_orchestrator.sdk.composition import build_deps
from navigator_orchestrator.sdk.context import Blocked, Ctx, FileAccess
from navigator_orchestrator.sdk.graph import resume_template_graph, run_template_graph
from navigator_orchestrator.sdk.judge import JudgeError, judges_for, load_judges
from navigator_orchestrator.sdk.loader import LoadError, load_file
from navigator_orchestrator.sdk.project import (
    Project,
    ProjectError,
    find_manifest,
    load_project,
    load_project_templates,
    parse_project,
)
from navigator_orchestrator.sdk.runner import RunResult, StepFailed, run_template
from navigator_orchestrator.sdk.schema import (
    SchemaContractError,
    make_schema_snapshot,
    validate_instance,
)
from navigator_orchestrator.sdk.schema_sources import load_locked_schema, sync_schema
from navigator_orchestrator.sdk.templates import Template, TemplateRegistry
from navigator_orchestrator.store.events import FileEventLog
from navigator_orchestrator.templates import default_registry

__all__ = ["main"]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _parse_params(extras: Sequence[str]) -> dict[str, Any]:
    """Turn `--question "x" --dir ./docs` into `{"question": "x", "dir": "./docs"}`."""
    params: dict[str, Any] = {}
    index = 0
    while index < len(extras):
        token = extras[index]
        if not token.startswith("--"):
            raise SystemExit(f"unexpected argument {token!r}; parameters look like --name value")
        name = token[2:].replace("-", "_")
        if "=" in name:
            name, _, inline = name.partition("=")
            params[name] = inline
            index += 1
            continue
        if index + 1 >= len(extras) or extras[index + 1].startswith("--"):
            params[name] = True  # a bare --flag is a boolean
            index += 1
            continue
        params[name] = extras[index + 1]
        index += 2
    return params


def _load_params_file(path: str) -> dict[str, Any]:
    """Read run parameters from a JSON or YAML file.

    A flow with eight inputs is unreadable as flags, and a file can be
    committed, reviewed and re-run — which is what turns "the thing Akio ran on
    Monday" into something anyone can reproduce.
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SystemExit(f"cannot read params file {target}: {exc}") from exc

    try:
        if target.suffix.lower() in (".yaml", ".yml"):
            import yaml  # noqa: PLC0415 - only needed for this branch

            loaded = yaml.safe_load(text) or {}
        else:
            loaded = json.loads(text)
    except Exception as exc:
        # Name the file and the parse error. "Expecting ',' delimiter: line 4"
        # without a filename is a puzzle rather than a message.
        raise SystemExit(f"{target}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise SystemExit(f"{target}: expected a mapping of parameters, got {type(loaded).__name__}")
    return {str(key).replace("-", "_"): value for key, value in loaded.items()}


def _project_for(path: str, registry: TemplateRegistry) -> Project | None:
    """The workflow project the file belongs to, if any.

    Searched from the **workflow file** rather than the working directory,
    so `navigator-orchestrator check editorial/flows/respond.py` from the repo root finds
    `editorial/navigator-orchestrator.toml` — which is the invocation CI will use.

    `None` outside a project, deliberately: running a workflow that calls no
    API needs no manifest, and demanding one would make the tutorial harder
    than the thing it teaches.
    """
    manifest = find_manifest(Path(path).expanduser().resolve().parent)
    if manifest is None:
        return None
    project = parse_project(manifest)
    load_project_templates(project, registry)
    return project


def _load_and_check(
    path: str, registry: TemplateRegistry, project: Project | None = None
) -> tuple[Any, Template]:
    parsed, _module = load_file(path)
    template = check_file(parsed, registry, project)
    return parsed, template


def _describe(template: Template, judges: Sequence[Any] = ()) -> str:
    """The workflow's shape, judges included.

    Judges are listed because they are otherwise invisible: they live in a YAML
    file nobody edited and guard a step nobody changed. A compliance gate you
    cannot see without running the workflow is one people forget is there —
    which cuts both ways, since they also forget when it *stops* being there.
    """
    lines = [f"  {template.name} — {template.doc}"]
    for step in template.steps:
        for judge in [j for j in judges if step.name in j.before]:
            lines.append(f"      ├ judge {judge.describe()}")
        marker = "required" if step.required else "optional"
        takes = ", ".join(step.kwargs) or "—"
        lines.append(f"    {step.name:<10} [{step.executor}] {marker}; takes: {takes}")
        for judge in [j for j in judges if step.name in j.after]:
            lines.append(f"      └ judge {judge.describe()}")
    return "\n".join(lines)


def _cmd_check(args: argparse.Namespace, registry: TemplateRegistry) -> int:
    project = _project_for(args.file, registry)
    _parsed, template = _load_and_check(args.file, registry, project)
    where = f" in {project.root.name}/" if project else ""
    print(f"ok — {Path(args.file).name} is a valid '{template.name}' workflow{where}")
    print(_describe(template, _judges_for_run(project, template.name, _parsed)))
    return 0


def _cmd_run(args: argparse.Namespace, registry: TemplateRegistry, extras: Sequence[str]) -> int:
    project = _project_for(args.file, registry)
    parsed, template = _load_and_check(args.file, registry, project)
    # A params file is a **default**, not a mandate: an explicit flag wins, so
    # `make respond params=monday.json request_id=RQ1` overrides one value
    # without copying the file to change it.
    params = {**(_load_params_file(args.params) if args.params else {}), **_parse_params(extras)}

    deps = build_deps()
    deps.prompts.validate_all(template.prompt_refs) if deps.prompts else None

    # `--dir` **is** the workflow's world, so it becomes the root that hooks are
    # confined to. The sandbox exists to stop a *hook* wandering, not to argue
    # with the operator who named the corpus on the command line — rooting at
    # the working directory instead made `--dir ../elsewhere` a refusal, which
    # is the sandbox misfiring on the person it is supposed to serve.
    root = Path(params.get("root") or params.get("dir") or Path.cwd()).expanduser()
    ctx = Ctx(params=params, deps=deps, files=FileAccess(root=root), project=project)

    events = FileEventLog(root=_runs_dir())
    attached = _judges_for_run(project, template.name, parsed)
    needs_graph = bool(attached) or _has_gate(template)

    # The graph runner whenever the template has a judge or a gate. A judge is
    # an ordinary node and the sequential runner has no nodes to be ordinary
    # among; a gate needs `interrupt`, which a `for` loop cannot offer.
    if needs_graph:
        result = asyncio.run(
            _run_with_checkpointer(template, parsed.hooks, ctx, events=events, judges=attached)
        )
    else:
        result = asyncio.run(
            run_template(template, parsed.hooks, ctx, events=events, actor=_actor())
        )
    _report(result, verbose=args.verbose)
    _report_pause(result, args.file)
    print(
        f"\nrun {result.run_id}  ->  navigator-orchestrator runs {result.run_id}", file=sys.stderr
    )
    return 0


def _report_pause(result: RunResult, file: str) -> None:
    """Say — loudly — that a run is waiting for a person, and how to answer it.

    Without this the CLI printed the last product and exited 0, which reads
    exactly like success. A human-in-the-loop workflow whose pause is
    indistinguishable from completion is a workflow whose humans never arrive:
    the first real `make respond` looked finished and was not.
    """
    if not result.is_paused:
        return
    print(f"\nPAUSED at '{result.paused_at}' — waiting for a human decision.", file=sys.stderr)
    for line in _gate_summary(result):
        print(f"  {line}", file=sys.stderr)
    print("\nresume with one of:", file=sys.stderr)
    for verdict in ("approve", "reject", "revise"):
        print(
            f"  navigator-orchestrator decide {file} {result.run_id} --{verdict}", file=sys.stderr
        )


def _gate_summary(result: RunResult) -> list[str]:
    """A few lines about what is waiting, without dumping the payload.

    The engine does not interpret a gate payload (`SPEC-AIP-003` §3.3), so this
    reports its *shape* — which keys, and a title if one happens to be there —
    rather than pretending to understand it.
    """
    gate = result.gate or {}
    lines = [str(gate.get("doc") or "")] if gate.get("doc") else []
    payload = gate.get("payload") or {}
    for key, value in payload.items():
        if isinstance(value, dict):
            title = value.get("title") or value.get("name")
            lines.append(f"{key}: {title}" if title else f"{key}: {len(value)} fields")
        elif isinstance(value, (list, tuple)):
            lines.append(f"{key}: {len(value)} items")
        else:
            lines.append(f"{key}: {str(value)[:80]}")
    return lines


def _judges_for_run(project: Project | None, template_name: str, parsed: Any) -> list[Any]:
    """Judges attached to this template, minus any the workflow file disabled.

    `SKIP_JUDGES = ("sanctions@1",)` in a workflow file is deliberately ugly:
    skipping a compliance judge should look like a decision in a diff, not a
    convenience (SPEC-NSP-004 §5).
    """
    if project is None:
        return []
    skip = tuple(getattr(parsed, "skip_judges", ()) or ())
    return judges_for(load_judges(project.path("judges")), template_name, skip)


def _has_gate(template: Template) -> bool:
    return any(step.executor == "gate" for step in template.steps)


def _checkpoint_path() -> str:
    """One SQLite file per project, beside the run logs.

    Durable with no server (`SPEC-NSP-003` §2.2). It has to be a *file* rather
    than memory for the property that matters: the process may exit at a gate
    and a different one — tomorrow, on another machine — resumes the same run.
    """
    return os.environ.get("NAVIGATOR_CHECKPOINTS") or str(_runs_dir().parent / "checkpoints.sqlite")


async def _run_with_checkpointer(
    template: Template,
    hooks: dict[str, Any],
    ctx: Ctx,
    *,
    events: FileEventLog,
    judges: Sequence[Any],
) -> RunResult:
    async with checkpointer_scope("sqlite", _checkpoint_path()) as saver:
        return await run_template_graph(
            template,
            hooks,
            ctx,
            events=events,
            actor=_actor(),
            checkpointer=saver,
            judges=judges,
        )


async def _resume_with_checkpointer(
    template: Template,
    hooks: dict[str, Any],
    ctx: Ctx,
    *,
    events: FileEventLog,
    judges: Sequence[Any],
    run_id: str,
    verdict: dict[str, Any],
) -> RunResult:
    async with checkpointer_scope("sqlite", _checkpoint_path()) as saver:
        return await resume_template_graph(
            template,
            hooks,
            ctx,
            verdict=verdict,
            events=events,
            run_id=run_id,
            actor=_actor(),
            checkpointer=saver,
            judges=judges,
        )


def _cmd_decide(args: argparse.Namespace, registry: TemplateRegistry) -> int:
    """Resume a paused run with a human decision (`SPEC-NSP-003`).

        navigator-orchestrator decide flows/respond.py 20260809-... --approve
        navigator-orchestrator decide flows/respond.py 20260809-... \
            --reject --comment "not sanctions"

    The workflow file is named again rather than remembered, because the graph
    is rebuilt from the template while the *state* comes from the checkpointer.
    Asking for it keeps that honest: resuming is running the same workflow
    again from where it stopped, not replaying a recording.
    """
    project = _project_for(args.file, registry)
    parsed, template = _load_and_check(args.file, registry, project)

    verdict = {
        "verdict": args.verdict,
        "actor": _actor(),
        "comment": args.comment or "",
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }

    deps = build_deps()
    root = Path(args.dir or Path.cwd()).expanduser()
    ctx = Ctx(params={}, deps=deps, files=FileAccess(root=root), project=project)
    events = FileEventLog(root=_runs_dir())
    attached = _judges_for_run(project, template.name, parsed)

    result = asyncio.run(
        _resume_with_checkpointer(
            template,
            parsed.hooks,
            ctx,
            events=events,
            judges=attached,
            run_id=args.run_id,
            verdict=verdict,
        )
    )
    _report(result, verbose=args.verbose)
    if result.is_paused:
        print(f"\nstill paused at {result.paused_at}", file=sys.stderr)
    return 0


def _runs_dir() -> Path:
    return Path(
        os.environ.get("NAVIGATOR_RUNS_DIR") or Path.cwd() / ".navigator-orchestrator" / "runs"
    )


def _actor() -> str:
    """Who ran it. A real principal arrives with sign-in; until then, the OS user
    — which is honest about being an assertion rather than an authentication."""
    return os.environ.get("NAVIGATOR_ACTOR") or os.environ.get("USERNAME") or "system"


_MARK = {
    "ok": "ok  ",
    "skipped": "skip",
    "failed": "FAIL",
    "blocked": "STOP",
    "started": "... ",
    "awaiting_human": "wait",
}


def _cmd_runs(args: argparse.Namespace) -> int:
    """Show what happened, without reading logs (SPEC-EDW-002 §5)."""
    log = FileEventLog(root=_runs_dir())

    if not args.run_id:
        ids = log.run_ids()
        if args.waiting:
            ids = [run_id for run_id in ids if _run_status(log.read(run_id)) == "awaiting_human"]
        if not ids:
            message = "no runs are waiting for a human" if args.waiting else "no runs recorded"
            print(f"{message} under {_runs_dir()}")
            return 0
        print(f"{'RUN':<44} {'WORKFLOW':<18} {'STATUS':<8} STEPS")
        for run_id in ids[-args.limit :]:
            events = log.read(run_id)
            terminal = [e for e in events if e["step"] == "run" and e["status"] != "started"]
            status = terminal[-1]["status"] if terminal else "running"
            done = sum(1 for e in events if e["step"] != "run" and e["status"] in {"ok", "skipped"})
            workflow = events[0]["workflow"] if events else "?"
            print(f"{run_id:<44} {workflow:<18} {status:<8} {done}")
        return 0

    events = log.read(args.run_id)
    if not events:
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 2
    print(f"run {args.run_id}   workflow: {events[0]['workflow']}   actor: {events[0]['actor']}")
    print("-" * 70)
    for event in events:
        if event["status"] == "started" and event["step"] != "run":
            continue  # the terminal row carries the outcome; showing both doubles the noise
        detail = ", ".join(f"{k}={v}" for k, v in (event.get("detail") or {}).items())
        mark = _MARK.get(event["status"], event["status"])
        print(f"  {mark}  {event['step']:<12} {event['at'][11:19]}  {detail[:70]}")
    return 0


def _run_status(events: Sequence[dict[str, Any]]) -> str:
    """Latest externally meaningful run status from an append-only event stream."""
    terminal = [e for e in events if e.get("step") == "run" and e.get("status") != "started"]
    return str(terminal[-1]["status"]) if terminal else "running"


async def _checkpoint_values(run_id: str) -> dict[str, Any]:
    """Read the latest durable pool without resuming or executing the graph."""
    async with checkpointer_scope("sqlite", _checkpoint_path()) as saver:
        assert saver is not None  # sqlite always yields a saver
        saved = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
    if saved is None:
        return {}
    values = saved.checkpoint.get("channel_values", {})
    pool = values.get("pool", {}) if isinstance(values, dict) else {}
    return dict(pool) if isinstance(pool, dict) else {}


def _cmd_show(args: argparse.Namespace) -> int:
    """Show exactly what the latest human gate asked the reviewer to decide."""
    events = FileEventLog(root=_runs_dir()).read(args.run_id)
    if not events:
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 2

    status = _run_status(events)
    if status != "awaiting_human":
        print(
            f"run {args.run_id} is {status}, not awaiting a human; "
            f"use `navigator-orchestrator runs {args.run_id}`",
            file=sys.stderr,
        )
        return 2

    waits = [e for e in events if e.get("status") == "awaiting_human" and e.get("step") != "run"]
    if not waits:
        print(f"run {args.run_id} has no human review gate", file=sys.stderr)
        return 2

    gate = waits[-1]
    keys = gate.get("detail", {}).get("keys", [])
    pool = asyncio.run(_checkpoint_values(args.run_id))
    payload = {str(key): pool[key] for key in keys if key in pool}
    if not payload:
        print(f"run {args.run_id} has no durable review payload", file=sys.stderr)
        return 2

    print(f"run {args.run_id}   review: {gate.get('step')}   status: {status}")
    doc = gate.get("detail", {}).get("doc")
    if doc:
        print(str(doc))
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def _schema_project(args: argparse.Namespace) -> Project:
    start = Path(args.project).expanduser() if args.project else Path.cwd()
    return load_project(start)


def _read_payload(path: str) -> Any:
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8-sig")
        if target.suffix.lower() in {".yaml", ".yml"}:
            import yaml  # noqa: PLC0415

            return yaml.safe_load(text)
        return json.loads(text)
    except (OSError, ValueError) as exc:
        raise SchemaContractError(f"cannot read {target}: {exc}") from exc


def _cmd_schema(args: argparse.Namespace) -> int:
    project = _schema_project(args)
    if args.schema_command == "sync":
        snapshot = sync_schema(project, args.name)
        print(f"synced {args.name}  sha256:{snapshot.revision}")
        return 0

    snapshot = load_locked_schema(project, args.name)
    if args.schema_command == "show":
        print(
            json.dumps(
                {
                    "id": snapshot.ref.id,
                    "revision": snapshot.revision,
                    "dialect": snapshot.dialect,
                    "source": snapshot.ref.model_dump(mode="json"),
                    "schema": snapshot.schema_,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.schema_command == "validate":
        result = validate_instance(snapshot, _read_payload(args.file))
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0 if result.valid else 1

    comparison = _read_payload(args.against)
    if not isinstance(comparison, dict):
        raise SchemaContractError(f"comparison schema {args.against} is not an object")
    # Validate the comparison as a schema before describing it as drift.
    make_schema_snapshot(snapshot.ref.model_copy(update={"revision": None}), comparison)
    changes = _schema_diff(snapshot.schema_, comparison)
    print(
        json.dumps(
            {"schema_id": args.name, "drift": bool(changes), "changes": changes},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 1 if changes else 0


def _schema_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[dict[str, Any]] = []
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys - after_keys):
            changes.append({"path": _child_pointer(path, key), "change": "removed"})
        for key in sorted(after_keys - before_keys):
            changes.append({"path": _child_pointer(path, key), "change": "added"})
        for key in sorted(before_keys & after_keys):
            changes.extend(_schema_diff(before[key], after[key], _child_pointer(path, key)))
        return changes
    if before == after:
        return []
    return [{"path": path, "change": "changed", "before": before, "after": after}]


def _child_pointer(path: str, child: object) -> str:
    escaped = str(child).replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _report(result: RunResult, *, verbose: bool) -> None:
    if verbose:
        for record in result.steps:
            print(f"  · {record.step:<10} [{record.executor}] {record.source}")
        for note in result.notes:
            print(f"  · note: {note}")
        print()
    output = result.output
    print(output if isinstance(output, str) else repr(output))


def _readable_output() -> None:
    """Make non-ASCII output survive a Windows console.

    The default console encoding here is cp1252, and an em dash in a report
    raises UnicodeEncodeError *while reporting* — so the run's own summary
    becomes the thing that crashes. `errors="replace"` keeps the report
    readable rather than fatal.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:  # pragma: no branch - always present on 3.7+
            # A redirected pipe may refuse; readable output is a nicety, and a
            # nicety must never be the thing that fails the command.
            with suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911,PLR0912,PLR0915 - explicit CLI dispatch preserves each exit-code contract
    _readable_output()
    parser = argparse.ArgumentParser(
        prog="navigator-orchestrator",
        description="Check and run workflow files.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="validate a workflow file without running it")
    check.add_argument("file")

    runs = sub.add_parser("runs", help="what happened, without reading logs")
    runs.add_argument("run_id", nargs="?", help="omit to list; supply to show one run")
    runs.add_argument("--limit", type=int, default=20)
    runs.add_argument("--waiting", action="store_true", help="only runs awaiting a human")

    show = sub.add_parser("show", help="show the durable payload presented at a human gate")
    show.add_argument("run_id")

    schema = sub.add_parser("schema", help="synchronize and validate runtime write contracts")
    schema.add_argument("--project", default="", help="project directory (default: current)")
    schema_sub = schema.add_subparsers(dest="schema_command", required=True)
    schema_sync = schema_sub.add_parser("sync", help="fetch and lock a configured contract")
    schema_sync.add_argument("name")
    schema_show = schema_sub.add_parser(
        "show", help="print a locked contract without a network call"
    )
    schema_show.add_argument("name")
    schema_validate = schema_sub.add_parser("validate", help="validate JSON/YAML against a lock")
    schema_validate.add_argument("name")
    schema_validate.add_argument("file")
    schema_diff = schema_sub.add_parser("diff", help="compare a lock with a schema file")
    schema_diff.add_argument("name")
    schema_diff.add_argument("--against", required=True, metavar="FILE")

    run = sub.add_parser("run", help="check, then run a workflow file")
    run.add_argument("file")
    run.add_argument("-v", "--verbose", action="store_true", help="show each step as it runs")
    run.add_argument(
        "--params",
        metavar="FILE",
        help="JSON or YAML file of run parameters; --flags override it",
    )

    decide = sub.add_parser("decide", help="resume a run paused at a human gate")
    decide.add_argument("file", help="the same workflow file the run started from")
    decide.add_argument("run_id")
    decide.add_argument("--comment", default="", help="why; recorded in the event log")
    decide.add_argument("--dir", default="", help="workflow root, if the flow reads files")
    decide.add_argument("-v", "--verbose", action="store_true")
    stance = decide.add_mutually_exclusive_group(required=True)
    for name in ("approve", "reject", "revise"):
        stance.add_argument(
            f"--{name}", dest="verdict", action="store_const", const=name, help=f"{name} it"
        )

    known, extras = parser.parse_known_args(argv)
    registry = default_registry()

    try:
        if known.command == "check":
            if extras:
                raise SystemExit(f"`check` takes no parameters; got {' '.join(extras)}")
            return _cmd_check(known, registry)
        if known.command == "runs":
            return _cmd_runs(known)
        if known.command == "show":
            if extras:
                raise SystemExit(f"`show` takes no parameters; got {' '.join(extras)}")
            return _cmd_show(known)
        if known.command == "schema":
            if extras:
                raise SystemExit(f"`schema` takes no extra parameters; got {' '.join(extras)}")
            return _cmd_schema(known)
        if known.command == "decide":
            if extras:
                raise SystemExit(f"`decide` takes no parameters; got {' '.join(extras)}")
            return _cmd_decide(known, registry)
        return _cmd_run(known, registry, extras)
    # Two failure classes, two exit codes, because they need different reactions:
    #   2 — the file or the invocation is wrong and **nothing ran**. Safe to
    #       wire into CI or a pre-commit hook.
    #   1 — the run started and stopped. Something happened; look at what.
    except CheckError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (LoadError, ProjectError, JudgeError, SchemaContractError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (Blocked, StepFailed) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
