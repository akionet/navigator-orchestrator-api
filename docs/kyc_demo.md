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

```bash
uv run --project ../.. python demo_python.py CL-0001
```

```
CL-0001  Anneliese Vogt
  tier             : premium
  PEP              : False
  sanctions country: DE (legal residency)
  adverse media    : 0 implicating
  pep_gate         : {'verdict': 'not_required', 'reason': 'pep.is_pep is not set for this run'}
  art_gate         : {'verdict': 'not_required', 'reason': 'eligibility.has_art is not set for this run'}
```

`demo_python.py` is ~40 lines and uses only public API. This is the mode for a
service, a notebook or a batch job — the CLI is a thin wrapper over it.

**One trap worth knowing:** use `run_template_graph`, **not** `run_template`. The
sequential runner has nothing to interrupt, so it treats a gate as a step needing
a hook and fails with `required hook is not implemented`. Any workflow with a
gate needs the graph runner, which is also what gives it a checkpointer.

```bash
uv run --project ../.. python demo_python.py CL-0004
# paused at pep_gate: a PEP match is a decision a compliance officer owns, not a rejection
```

Resuming across processes is the CLI's job — see below.

---

## 2. CLI — the operator's path, step by step

**Validate before running.** Checks the workflow file against the template
without executing anything:

```bash
navigator-orchestrator check flows/kyc.py
# ok — kyc.py is a valid 'kyc-onboarding' workflow in kyc/
```

**Run the clean case** — completes in one command:

```bash
navigator-orchestrator run flows/kyc.py --client_id CL-0001
```

**Run a PEP** — stops for a human:

```bash
navigator-orchestrator run flows/kyc.py --client_id CL-0004
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
navigator-orchestrator runs                     # the queue
navigator-orchestrator show <run-id>            # the durable gate payload
```

**Decide** — this can be a different person, in a different shell, after a
reboot. The payload is on disk, not in the first process's memory:

```bash
navigator-orchestrator decide flows/kyc.py <run-id> --approve --comment "cleared: no adverse findings"
```

The outcome carries the decision with actor, comment and timestamp.

**The two that prove the rules:**

```bash
navigator-orchestrator run flows/kyc.py --client_id CL-0007
# error: client CL-0007 has no country on its address; country-scoped
#        sanctions screening cannot be performed

navigator-orchestrator run flows/kyc.py --client_id CL-0008
# ... error: sanctions screening declines this client: IR is COMPREHENSIVE
```

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

Run history is in-memory per process, and CLI runs are on disk under the project.
`navigator-orchestrator runs` lists them; deleting the runs directory clears the
slate if the queue gets noisy mid-demo.
