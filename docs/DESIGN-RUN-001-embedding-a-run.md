# DESIGN-RUN-001 — Embedding a run

How code other than the CLI starts a workflow. Today the only supported answer
is "read `cli.py` and copy it", which is how `workflows/kyc/demo_python.py`
ended up hardcoded and verbose.

## 1. What mature runtimes actually do

Neither Airflow nor Temporal is well described as sync or async. Both are
**durable execution with a client/worker split**: you submit, you get a handle,
and the run outlives the caller.

- **Airflow** — the authoring surface is plain sync Python, tasks execute in
  other processes, and triggering a DAG run returns immediately. The `asyncio`
  in the triggerer is an efficiency detail for long waits, not the model.
- **Temporal** — the Python SDK is asyncio-native, but the notable part is that
  workflow code *looks* sequential while being durably suspended and replayed.
  `start_workflow()` returns a handle; `execute_workflow()` is the convenience
  that blocks.

The lesson is not the keyword. It is **submit → handle → observe**, with
durability in the middle.

This engine is already shaped that way: a checkpointer, a run id, and `decide`
resuming from a different process after a reboot. The missing piece is the
worker — the same gap that stops `kyc-onboarding` appearing in the console.

## 2. Decisions

### 2.1 Async core, sync facade

`run_template_graph` is already `async def` and the CLI wraps it in
`asyncio.run`. That precedent stands: LangGraph and httpx require an async core,
and most callers embedding a run are scripts, notebooks and batch jobs that are
not async. Forcing `asyncio.run` on them is friction with no payoff.

```python
def       run_workflow(name, **params) -> RunOutcome        # blocks; common case
async def arun_workflow(name, **params) -> RunOutcome       # async-native callers
def       start_workflow(name, **params) -> RunOutcome      # returns at the first pause
def       run_batch(name, params_list)   -> list[RunOutcome]
```

`run_workflow` is `arun_workflow` under `asyncio.run`, and nothing else.

### 2.2 A pause is a return value, not an exception

The decisive point. Gates mean a run legitimately may not complete. If the call
only returns on completion and raises otherwise, every caller wraps the *normal*
case in `try/except`.

Airflow models this as a DAG-run state; Temporal as a workflow blocked on a
signal. So does this:

```python
outcome.status   # completed | paused | declined | failed
outcome.run_id   # always present — the handle for resuming
```

`run_id` is populated for every status including `failed`, because a run that
failed is still a run somebody has to look at.

### 2.3 `declined` is not `failed`

`ctx.require()` currently raises `Blocked` for two unrelated things:

- **CL-0007**, no country on the address — a *data error*. Someone must fix the
  record.
- **CL-0008**, sanctions decline — a *correct business outcome*. Nothing is
  broken; the control worked.

Collapsing them means "declined 40 clients this week" is indistinguishable from
"40 crashes" to anything watching. Airflow and Temporal both separate task
failure from business result because you alert on one and not the other.

`Declined` is therefore its own exception, raised by `ctx.decline(reason)`, and
its own status. `Blocked` keeps its meaning: the run could not proceed.

### 2.4 Batch returns outcomes, not results

A batch of five may complete three, pause one and decline one. A list of results
cannot express that; a list of `RunOutcome` can, and mirrors a list of Airflow
DAG-run states. One decline must not stop the rest — `make batch` already
behaves this way and the API should match.

## 3. Not in scope

- **The worker.** `start_workflow` returns at the first pause; it does not hand
  off to another process. Real submit-and-forget needs the worker, and that is
  its own piece of work.
- **Scheduling, retries, backfill.** Airflow's actual job. Out of scope here.
- **Resuming from the API.** `decide` remains the CLI's; the facade reports a
  pause and the run id rather than growing a second resume path.

## 4. Status

`proposed` — implementation started. `sdk/run.py` carries the facade and
`RunOutcome`; `demo_python.py` stays as a worked example of the lower-level API
rather than being the interface.
