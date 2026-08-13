# DESIGN-WRK-001 — The worker

The gap behind two separate symptoms:

- `kyc-onboarding` cannot appear in the operator console, only `echo` and
  `approval`.
- `start_workflow` cannot exist, because there is nothing to hand a run to.

Both are the same hole. This is the design for filling it.

## 1. Why the obvious fix is wrong

The console lists `WorkflowRegistry`, which holds `Workflow` classes. The SDK's
`TemplateRegistry` holds templates and workflow files. The tempting fix is to
have the API read the SDK registry.

`tests/test_sdk_isolation.py` fails the build if it does, and that test is
right. The API is a long-lived, potentially multi-tenant HTTP service. A
workflow file is **arbitrary user Python, executed on import** — `loader.py`
says so plainly. Importing definitions into the API process means any workflow
author has code execution inside the request path of every other tenant.

So the rule stands: **the API never loads, binds or executes definition code.**
Everything below is arranged to keep that true.

## 2. The shape

```
  operator                API process              worker process
     │                         │                        │
     │  POST /workflows/kyc-onboarding/runs             │
     │────────────────────────>│                        │
     │                         │  create_run(queued)    │
     │                         │───────────────────────>│  claims it
     │                         │                        │  loads the project
     │                         │                        │  runs the template
     │                         │<───────────────────────│  mark_state, log
     │  GET .../runs/{id}      │                        │
     │────────────────────────>│                        │
```

The API owns HTTP, identity and the stores. The worker owns execution. They
share **data**, never imports.

## 3. Three pieces

### 3.1 A workflow descriptor, so the API can list what it cannot import

The console needs a name, an input schema and a source kind. It does not need
the code. A worker publishes a descriptor at startup; the API reads it from the
store and serves it through the existing `/workflows` endpoint.

```python
class WorkflowDescriptor(BaseModel):
    name: str                       # "kyc-onboarding"
    input_schema: dict[str, Any]    # derived from Template.params
    source_kind: Literal["yaml", "python"]
    checkpointed: bool
    worker: str                     # which worker can run it
    registered_at: datetime
```

This is the piece that makes the console generic rather than aware of KYC.
`WorkflowSummary` already has this shape — it gains a source and loses the
assumption that the registry holds classes.

**Resolved.** `Template.params` now accepts `Param(name, type, required, doc,
default)` and `Template.input_schema()` derives real JSON Schema from it, so a
descriptor publishes the same grade of schema as `echo` and `approval`. A bare
string is still shorthand for a required string, so typing is opt-in per
parameter and no existing template broke.

`Template.publishes` and `result_schema` do the same for the other edge: which
pool key is the result, and a ref to its shape in `[schemas.*]`. Both optional.

What is deliberately still untyped is everything **between** the steps. Rigid
structure between agentic nodes is self-defeating — the useful outputs are
frequently the ones no schema anticipated — so the descriptor describes the two
edges and says nothing about the pool. `tests/test_param_schema.py` asserts that
no schema field appears on the general step contract, which turns the principle
into a build failure rather than a convention.

### 3.2 A queue the API can write and a worker can claim

`RunStore` already carries `create_run`, `mark_state`, `get_run` and
`list_runs`. A queue needs one more thing: **atomic claiming**, so two workers
cannot take the same run.

```python
async def claim_run(self, worker: str, workflows: Sequence[str]) -> RunRecord | None:
    """Atomically move one `queued` run to `running` and return it."""
```

`RunState` gains `queued`. In Postgres that is `SELECT … FOR UPDATE SKIP LOCKED`;
in the in-memory store it is the existing lock. Nothing else in the protocol
changes, which is the point — the store already models runs, states and
decisions, and this is one more transition.

### 3.3 The worker loop

```
claim → load project → run_template_graph → mark_state(+gate payload) → repeat
```

On a gate, the worker writes `awaiting_decision` with the payload and stops
touching the run. A decision arriving through the API sets it back to `queued`
with the verdict attached, and a worker — **not necessarily the same one** —
claims it and resumes from the checkpointer. That is separation of duties
already working at the CLI, moved behind HTTP.

## 4. What this deliberately is not

**Not a scheduler.** No cron, no backfill, no dependency graph between runs.
Airflow's actual job, and out of scope.

**Not distributed durability.** One process claiming from a shared store is
enough to unblock the console and `start_workflow`. Retries, heartbeats, lease
expiry and poison-message handling are real and deferred — with the honest note
that a worker crashing mid-run currently leaves a run `running` forever.
A lease timestamp on `claim_run` is the smallest fix and should land with the
Postgres store.

**Not a sandbox.** The worker executes user Python by design. It is the
*blast radius*, not a defence: it should run with its own credentials, its own
network policy, and no access to the API's secrets. Moving execution out of the
API process is what makes that possible; it is not automatically what makes it
safe.

## 5. Sequencing

| | Piece | Unblocks |
|---|---|---|
| 1 | `queued` state + `claim_run` on the in-memory store | nothing yet, but everything depends on it |
| 2 | `WorkflowDescriptor` + descriptor store, served by `/workflows` | **KYC appears in the console** |
| 3 | The worker loop, run as a separate process | **runs actually execute** |
| 4 | `start_workflow` returning a handle | the API in `DESIGN-RUN-001` |
| 5 | Lease expiry, retries, Postgres store | production |

Steps 1–3 are the demo-visible ones. Step 5 is where this stops being a POC.

## 6. The test that must keep passing

`test_sdk_isolation.py`, unchanged. If the worker is built correctly, the API
gains the ability to *list and start* workflows it still cannot import. That
test failing at any point means the design has been abandoned rather than
implemented.

A second test should assert the descriptor path carries **no callables** — a
descriptor that can smuggle a hook back into the API process defeats the whole
arrangement.

## 7. Status

`in progress`.

**Step 1 is done.** `RunState` carries `queued`; `RunStore` carries
`claim_run`, `renew_lease` and `reclaim_expired_leases`. A claim is a lease, and
`claim_run` reclaims expired ones before claiming, so the queue heals without a
scheduler and without the worker being asked to notice its own death. Covered by
`tests/test_run_queue.py`.

**Steps 2 and 3 should land together.** Listing a workflow the runtime cannot
yet execute gives an operator a console entry that fails on launch, which is
worse than its absence — a demo where clicking the interesting thing returns
`501` is harder to explain than one where it is honestly not there.

The open question before starting them is §3.1: `Template.params` is a bare
tuple of names with no types, so the first descriptor's `input_schema` is
names-only and thinner than the Pydantic schema `echo` and `approval` publish.
Either the console tolerates two grades of schema, or param typing lands first.
