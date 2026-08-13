# navigator-orchestrator-api

A model-agnostic AI workflow engine. Workflows are registered graphs with typed
inputs and outputs, injected tools and model clients, versioned prompts, and
optional human-in-the-loop gates that survive the process exiting.

> **Status: pre-release.** The engine is here and its checks are green, but this
> is not published yet — the licence is unreviewed and the KYC reference workflow
> is design rather than running code. [`EXTRACTION.md`](EXTRACTION.md) records
> what was removed on the way out of its original codebase, and the known gaps.

## Quick start

```bash
uv sync --group dev --extra server
cp .env.example .env    # defaults to the offline `fake:` model — no provider, no cost
uv run --extra server uvicorn navigator_orchestrator.api.app:app --port 8000
```

`make check` runs lint, `mypy --strict` and the full suite — exactly what CI
runs. The hermetic path installs no provider and reaches no network.

## The idea

Every use case is the same shape: *assemble context → run an agentic graph →
produce structured output at the edge → optionally pause for a human → emit a
result*. Reuse lives in the engine; each capability is a thin plugin.

Two principles do most of the work:

- **Schemas at the edges, free text inside.** Contracts are enforced on workflow
  I/O and tool calls, never forced onto intermediate reasoning.
- **Deterministic by default.** An agent is used where the input is unstructured
  and the judgement is genuinely open. Everything else is a rule, because a rule
  is reviewable as a diff and does not vary between runs.

## Workflows are definitions, not engine code

`src/navigator_orchestrator/` is the engine. `workflows/` holds definitions, and
the engine never reads that directory — it is handed a project via a manifest.
Both halves of that boundary are asserted rather than documented:
`tests/test_sdk_isolation.py` AST-scans imports, and
`tests/test_example_workflow_is_removable.py` fails if engine, test or CI code
learns a workflow's name.

**Start your own project by deleting the sample** — see
[`workflows/README.md`](workflows/README.md) for the extension model, including
how to inject plain Python functions as steps when no generic engine step fits.

## Reference workflow: KYC client onboarding

Client onboarding screening — adverse media, PEP, sanctions and eligibility
tiering. It was chosen because it is honest about the split above: eight
deterministic steps, two agents, two human gates.

- [`workflows/kyc/DESIGN.md`](workflows/kyc/DESIGN.md) — the design, the rules, and why each step is the kind it is
- [`workflows/kyc/flows/kyc-onboarding.yaml`](workflows/kyc/flows/kyc-onboarding.yaml) — the workflow
- [`workflows/kyc/judges/`](workflows/kyc/judges/) — the two agent configurations
- [`workflows/kyc/data/README.md`](workflows/kyc/data/README.md) — the fixture matrix

The interesting part is adverse media. A defence lawyer appears in every article
about their client's fraud, as do the investigator, the journalist and the
victims. Screening on name-match alone flags all of them, and a queue full of
false positives is a control that nobody reads. So extraction and adjudication
are two separate agents, and the adjudicator must assign the subject a *role*
before it can implicate them.

All fixture data is synthetic. No real person, company, register or sanctions
list is represented.

## Repositories

| | |
|---|---|
| `navigator-orchestrator-api` | the engine and workflows (Python, FastAPI) |
| `navigator-orchestrator-app` | the operator console (React, Vite, TypeScript) |

## Licence

Not yet chosen. Nothing here should be published until it is.
