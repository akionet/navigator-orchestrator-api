# KYC demo cheatsheet

Three ways to run the reference workflow. **Two of them work today** — the third
is honest about what is missing.

Assumes [`setup.md`](setup.md) is done. Everything below is offline and costs
nothing: confirm with `curl -s localhost:8000/healthz | jq .engine.model` →
`"fake:local"`.

```bash
cd workflows/kyc          # every command below runs from here
```

## The fixtures, and what each one shows

| Client | Shows | Result |
|---|---|---|
| **CL-0001** Anneliese Vogt | the clean path | completes, **premium**, no pause |
| **CL-0004** Dmitri Sokolov | a PEP match | pauses at `pep_gate` |
| **CL-0005** Elena Marchetti | art in the portfolio | skips PEP, pauses at `art_gate`, **UHNWI** |
| **CL-0007** Farida Nasser | missing country | **errors** — never a silent pass |
| **CL-0008** Viktor Aslanov | ordered jurisdiction rules | **declined**, Iran via the business, not Cyprus |

`CL-0001` is the smoothest opener. `CL-0008` is the one that makes the point.

---

## 1. Python — embed a run in your own code

Four calls are the whole API. Workflows are named, not pathed:

```python
from navigator_orchestrator import ids_from_file, outcomes_by_status, run_batch, run_workflow

outcome = run_workflow("kyc-onboarding", project_dir=HERE, client_id="CL-0001")
outcome.status        # completed | paused | declined | failed
outcome.run_id        # always present — the handle for resuming
outcome.output        # the screening record, when it completed
```

`demo_python.py` wraps that in a CLI:

```bash
uv run --project ../.. python demo_python.py CL-0001 CL-0004 CL-0005 CL-0007 CL-0008
```
```
CL-0001  completed
    tier: premium
CL-0004  paused     pep_gate
CL-0005  paused     art_gate
CL-0007  failed     client CL-0007 has no country on its address; ...
CL-0008  declined   sanctions screening declines this client: IR is COMPREHENSIVE

{'completed': 1, 'paused': 2, 'failed': 1, 'declined': 1}
```

Or from a file, one id per line:

```bash
uv run --project ../.. python demo_python.py --file ids.example.txt
```

### The two design points worth narrating

**A pause is a return value, not an exception.** Gates mean a run legitimately
may not complete, so raising would put the *normal* case in a `try/except` at
every call site. Airflow models this as a DAG-run state and Temporal as a
workflow blocked on a signal; `RunOutcome.status` is the same idea.

**`declined` is not `failed`.** CL-0007 has no country on its address — a data
error someone must fix. CL-0008 is a sanctions decline — the control *working*.
Both used to raise `Blocked` and were indistinguishable, which would make
"declined 40 clients this week" look identical to "40 crashes" to anything
watching. `ctx.decline()` now separates them.

A batch keeps going through both: one decline does not stop the rest.

See [`DESIGN-RUN-001`](DESIGN-RUN-001-embedding-a-run.md) for the reasoning, and
why `start_workflow` is deliberately absent until the worker exists.

Resuming across processes is still the CLI's job — see below.

---

## 2. CLI — the operator's path, step by step

The CLI is general; the project's `Makefile` names the verbs *this* workflow
has, so you type `make queue` rather than a flow path and a run id copied out
of earlier output. Start here:

```bash
make help
```
```
check     Validate the workflow file without running anything
screen    Screen one client: make screen client=CL-0001
batch     Screen several: make batch clients="CL-0001 CL-0004" | file=ids.txt
queue     What is waiting for a human decision
runs      Every run, most recent last
show      The durable payload a reviewer sees: make show run=<id>
approve   Approve a paused run: make approve run=<id> [why="..."]
reject    Reject a paused run: make reject run=<id> [why="..."]
revise    Send back for revision: make revise run=<id> [why="..."]
clean     Discard local run history
```

Copy that file into your own workflow project and rename the verbs — it is the
cheapest part of the whole design to change.

**Validate before running.** Checks the workflow file against the template
without executing anything:

```bash
make check
# ok — kyc.py is a valid 'kyc-onboarding' workflow in kyc/
# ... then the step list, so you can see what will run
```

**Screen the clean case** — completes in one command:

```bash
make screen client=CL-0001
```

**Screen a PEP** — stops for a human:

```bash
make screen client=CL-0004
```
```
PAUSED at 'pep_gate' — waiting for a human decision.
  a PEP match is a decision a compliance officer owns, not a rejection
  client: Dmitri Sokolov
  pep: 6 fields
resume with one of:
  navigator-orchestrator decide flows/kyc.py 20260813-...-kyc-onboarding-... --approve
```

**See what is waiting**, without reading logs:

```bash
make queue                    # only runs awaiting a human
make runs                     # everything
make show run=<id>            # the durable gate payload
```

**Decide** — this can be a different person, in a different shell, after a
reboot. The payload is on disk, not in the first process's memory:

```bash
make approve run=<id> why="cleared: no adverse findings"
make reject  run=<id> why="insufficient provenance"
make revise  run=<id> why="need the art historian's report"
```

The outcome carries the decision with actor, comment and timestamp.

**Screen several at once** — a list, or a file with one id per line:

```bash
make batch clients="CL-0001 CL-0004 CL-0005"
make batch file=ids.example.txt
```

A batch keeps going when a client is declined, so one rejection does not stop
the rest. Each still pauses individually if its gate is material — `make queue`
afterwards shows what needs a human.

**The two that prove the rules:**

```bash
make screen client=CL-0007
# error: client CL-0007 has no country on its address; country-scoped
#        sanctions screening cannot be performed

make screen client=CL-0008
# ... error: sanctions screening declines this client: IR is COMPREHENSIVE
```

<details>
<summary>The underlying CLI, if you would rather not use make</summary>

```bash
navigator-orchestrator check  flows/kyc.py
navigator-orchestrator run    flows/kyc.py --client_id CL-0001
navigator-orchestrator runs
navigator-orchestrator show   <run-id>
navigator-orchestrator decide flows/kyc.py <run-id> --approve --comment "cleared"
```

`NAVIGATOR_MODEL=fake:local` is exported by the Makefile; set it yourself if you
call the CLI directly, or the runtime falls back to whatever `.env` says.
</details>

`CL-0008` is worth narrating: his residency is Cyprus and unremarkable, but he
holds 74.5% of a shipper operating in Iran. Check residency first and he passes.
The rules are **ordered**, and the fixture exists to keep them that way.

---

## 3. GUI — not available for this workflow

The console at `navigator-orchestrator-app` will show `echo` and `approval`. It
will **not** show `kyc-onboarding`, and no amount of frontend work changes that.

There are two registries, and the gap between them is deliberate:

| | Runs via | Registry | Holds |
|---|---|---|---|
| SDK / CLI | `navigator-orchestrator run` | `TemplateRegistry` | templates + workflow files |
| Engine / API / GUI | `POST /workflows/{name}/runs` | `WorkflowRegistry` | `Workflow` classes |

`tests/test_sdk_isolation.py` **asserts the API never imports the SDK**, because
the API process must never load or execute definition code. Bridging them
directly fails the build, by design — if it inverted, arbitrary user Python
would be reachable from a multi-tenant service.

The intended answer is the **worker** the specs anticipate: the API dispatches, a
separate process executes definitions and reports back. It does not exist yet.
Implementing KYC a second time as a `Workflow` class would demo faster and is
exactly the duplication the boundary exists to prevent.

**For a GUI demo today**, use `approval` — it exercises the same human-gate
story end to end (pause, durable payload, a different actor deciding, audit
chain) with a workflow the runtime owns.

---

## What does not demo offline

The two adverse-media agents are inert under `fake:`, which echoes its prompt
rather than reasoning. `adverse_media_reviewed` is always `0`, so the two-agent
split and the defence-lawyer false-positive case (CL-0003) only show against a
real provider — see [`providers.md`](providers.md).

Everything else — the deterministic screening, both gates, the ordered
jurisdiction rules, the tiering arithmetic and the audit trail — is fully
exercised offline. That is most of the workflow, and it is the part that would
be wrong in a real system.

## Resetting

CLI runs are written to disk under the project, so the queue accumulates across
a demo. `make runs` lists them and `make clean` clears the slate if it gets
noisy mid-presentation.

The API runtime's history is separate and in-memory: restarting the server
discards it. Both facts are worth stating out loud rather than being caught by.

## Gates only stop when they matter

Every gate used to stop every run, so a reviewer confirmed several times a day
that someone was *not* a politically exposed person. A control people learn to
click through is worse than no control — the one real match arrives looking
exactly like the noise.

Gates now declare a condition: `pep_gate` fires on `pep.is_pep`, `art_gate` on
`eligibility.has_art`. A skipped gate is still a **recorded decision**, not an
absence:

```
'pep_gate': {'verdict': 'not_required', 'reason': 'pep.is_pep is not set for this run'}
```

"No human was asked, and here is why" is a different fact from "this workflow
has no gate", and an audit trail that cannot tell them apart is not one.

It **fails closed**: a condition naming a path no step produced — a typo, a
renamed output, a deleted step — pauses rather than skips. Silently dropping a
compliance control and leaving a clean record behind it is the worst available
failure mode.
