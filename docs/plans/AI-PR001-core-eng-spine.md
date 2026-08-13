---
id:          PLAN-AIP-R0-001
title:       R0 core engine spine — detailed implementation plan
realizes:    SPEC-AIP-002
status:      done              # proposed → approved (flips SPEC-AIP-002 to plan-approved) → in-progress → done
domain:      AIP
owner:       @akiocox
release:     R0
depends-on:  [SPEC-AIP-001, SPEC-AIP-002]
---

# PLAN-AIP-R0-001 — R0 core engine spine

Turns `SPEC-AIP-002` into an ordered, commit-by-commit build. **Approving this
plan** (status → `approved`) bumps `SPEC-AIP-002` to `plan-approved` and
authorizes code. Every step lists its files, the AC(s)/feature-ids it satisfies,
and a per-step definition of done. The `echo` workflow is the only "feature" — it
exists solely to make AC-1…AC-7 assertable with **zero business logic**.

## 0. Conventions

- **Python** 3.12, `uv` for deps/venv, `ruff` (lint+format), `mypy` (strict),
  `pytest` + `pytest-bdd`. One commit per step; trailer:
  `Spec: SPEC-AIP-002` / `Plan: PLAN-AIP-R0-001` / `Step: S<n>`.
- **Hermetic by default:** CI never calls a real model. `FakeClient` backs all
  BDD/unit tests. A single `@live` smoke is opt-in via `LIVE_LLM=true`.
- **Branch:** `claude/ai-helper-app-design-xgjffe` (per repo policy); one PR for R0.

## 1. Build steps (ordered by dependency)

### S1 — Repo scaffold & toolchain  *(foundation)*
- **Files:** `pyproject.toml` (deps from SPEC-AIP-002 §3.11), `ruff.toml`,
  `mypy.ini`, `.pre-commit-config.yaml`, `Makefile` (`make lint test typecheck`),
  `src/navigator_orchestrator/__init__.py`, `.github/workflows/ci.yml` (lint→typecheck→test),
  `.env.example`, `README.md`.
- **DoD:** `make lint test typecheck` green on an empty package; CI runs on push.

### S2 — Core contracts  *(TODO-1 · C-5, C-6)*
- **Files:** `engine/state.py` (`BaseState`), `engine/policy.py` (`Policy`),
  `engine/workflow.py` (`Workflow` ABC, `WorkflowRegistry`), `engine/deps.py`
  (`Deps` injection container).
- **Signatures:** as SPEC-AIP-002 §3.2. `Workflow.Input/Output` are Pydantic v2.
- **Tests:** input/output validation (valid + 422 paths); registry
  register/lookup/duplicate-name error.
- **DoD:** contracts unit-tested; `mypy --strict` clean.

### S3 — Injected LLM client + FakeClient  *(TODO-2 · C-4, AC-2)*
- **Files:** `engine/llm.py` (`LLMClient` Protocol, `make_client(policy)`,
  `FakeClient`, `BedrockClient`, `AnthropicClient` stub).
- **FakeClient:** deterministic — echoes/patterns from `policy` + input, streams
  N tokens; no network. Configurable canned responses for tests.
- **Tests:** factory dispatch by `model` prefix; **AC-2** — same node code runs
  under `fake:` and a second (stubbed `bedrock:`) provider with identical results.
- **DoD:** provider swap proven by test; no node imports a concrete client.

### S4 — Prompt registry  *(TODO-3 · C-7, AC-4)*
- **Files:** `engine/prompts.py` (`PromptRegistry.load("id@version")`,
  `validate_all()`), `prompts/echo/1.md` (front-matter: `id, version, inputs`).
- **Tests:** load + render; **AC-4** — missing/renamed prompt raises at
  `validate_all()` (boot), not at request time.
- **DoD:** boot validation wired into app startup (S8).

### S5 — Runner & streaming + echo workflow  *(TODO-1/7 · AC-1)*
- **Files:** `engine/runner.py` (`Runner.run` → `graph.astream_events` → SSE
  `token|node|error|final`; validates `Output` before `final`), `workflows/echo/`
  (`EchoInput`, `EchoOutput`, one node using injected `deps.llm`).
- **Tests:** **AC-1** end-to-end run emits ordered SSE events with a validated
  final; invalid input → 422 (no graph run); node exception → `error` event.
- **DoD:** echo runs through the Runner on FakeClient.

### S6 — Cache & checkpointer  *(TODO-4 · AC-6)*
- **Files:** `engine/cache.py` (Redis; key
  `sha256(name+normalized_input+policy)`), `engine/checkpoint.py` (Postgres
  checkpointer wiring, opt-in).
- **Tests:** **AC-6** — identical idempotent request returns cached result with
  **zero** FakeClient calls (assert call-count 0 on 2nd run); checkpointer
  resumable smoke (echo runs checkpointer-off).
- **DoD:** cache hit path asserted; Redis/PG via testcontainers or CI services.

### S7 — Observability  *(TODO-5 · AC-5)*
- **Files:** `engine/observability.py` (OTel spans per node/run, Langfuse
  callback, `CostMeter`), redaction hook (no-op at R0).
- **Tests:** **AC-5** — a run produces one span tree with per-node spans and
  exactly one cost-meter entry (in-memory OTel exporter in tests).
- **DoD:** trace + cost assertions green without external collectors.

### S8 — API gateway  *(TODO-6 · AC-7)*
- **Files:** `api/app.py` (FastAPI, lifespan calls `PromptRegistry.validate_all`),
  `api/routes.py` (`POST /workflows/{name}/runs` SSE, `GET /healthz`),
  `api/authz.py` (stub dependency), `config.py` (typed settings surface).
- **Tests:** **AC-7** — `/healthz` reports engine+PG+Redis; SSE route streams echo
  via `httpx` ASGI transport.
- **DoD:** app boots; fails fast if a prompt is missing (AC-4 integration).

### S9 — BDD harness + purity check + CI gate  *(TODO-7 · AC-3, all)*
- **Files:** `features/*.feature` (§2 below), `features/steps/`, `conftest.py`
  (FakeClient fixtures, ASGI client), a **purity test** (`tests/test_purity.py`)
  that AST-scans `workflows/**` nodes and fails on module-level client
  instantiation or in-place state mutation (**AC-3**).
- **DoD:** `pytest` (unit + BDD) green in CI; `@live` skipped unless `LIVE_LLM=true`.

## 2. BDD feature files (authored now; steps land in S9)

```gherkin
# features/engine-runtime.feature
@SPEC-AIP-002
Feature: Workflow runtime and streaming

  @F-AIP-R0-01
  Scenario: A registered workflow runs end-to-end and streams
    Given the "echo" workflow is registered
    And the model policy is "fake:echo"
    When I run "echo" with input {"text": "ping"}
    Then I receive streamed "token" events
    And a final event whose output validates against EchoOutput
    And the final output text is "ping"

  @F-AIP-R0-02
  Scenario: Swapping the model needs no node change
    Given the "echo" workflow is registered
    When I run "echo" with policy "fake:echo"
    And I run "echo" with policy "fake:echo-alt"
    Then both runs succeed with identical node code paths

  @F-AIP-R0-07
  Scenario: Health endpoint reports dependencies
    When I GET "/healthz"
    Then the status is 200
    And the body reports "engine", "postgres", "redis" states

# features/engine-contracts.feature
@SPEC-AIP-002
Feature: Edge contracts and node purity

  @F-AIP-R0-01
  Scenario: Invalid input is rejected before the graph runs
    When I run "echo" with input {"wrong": 1}
    Then the response is a 422 with contract errors
    And no graph node executed

  @F-AIP-R0-03
  Scenario: A node importing a global client fails the purity check
    Given a node module that instantiates an LLM client at import time
    When the purity check runs
    Then it reports a violation

# features/engine-prompts.feature
@SPEC-AIP-002
Feature: Versioned prompt registry

  @F-AIP-R0-04
  Scenario: A missing prompt fails fast at startup
    Given the app references prompt "echo@2" which does not exist
    When the app starts
    Then startup fails with a missing-prompt error
    And no request was served

# features/engine-cache.feature
@SPEC-AIP-002
Feature: Idempotent response cache

  @F-AIP-R0-06
  Scenario: Identical request is served from cache without a model call
    Given the "echo" workflow with caching enabled
    When I run "echo" with input {"text": "ping"} twice
    Then the second run returns the cached result
    And the model client was called exactly once

# features/engine-observability.feature
@SPEC-AIP-002
Feature: Tracing and cost metering

  @F-AIP-R0-05
  Scenario: A run emits a trace and one cost-meter entry
    When I run "echo" with input {"text": "ping"}
    Then a span tree with one span per node is exported
    And exactly one cost-meter entry is recorded for the run
```

## 3. CI pipeline (`.github/workflows/ci.yml`)

`ruff check` → `ruff format --check` → `mypy --strict` → `pytest` (unit + BDD,
Postgres + Redis as CI service containers). Hermetic: no model network. Green CI
is the R0 release gate.

## 4. Definition of done (R0 release)

- All of S1–S9 merged on the branch; one PR.
- `make lint test typecheck` and CI green; **AC-1…AC-7** each covered by a passing
  tagged scenario.
- `SPEC-AIP-002` moved `plan-approved → in-review` in the PR; TODO-1…7 checked
  with commit refs; `roadmap`/dashboard updated (R0 → COMPLETED dark).
- No business logic beyond `echo`. Forward-compat seams (capability metadata on
  `Tool`/`DataSource`; external/async HITL resolver) present as **protocol only**.

## 5. Sequencing & risk notes

- Critical path: S1→S2→S3→S5 (runnable engine) then S4/S6/S7/S8 parallelizable,
  S9 last. ~7 commits.
- **Risk:** LangGraph `astream_events` shape drift → isolate mapping in `runner.py`
  behind a small adapter so a version bump touches one file.
- **Risk:** Redis/PG in CI → use service containers (documented in `ci.yml`);
  tests skip with a clear message if unavailable locally.
- **Out of scope (later specs):** real auth, real Bedrock wiring beyond a smoke,
  eval datasets, any workflow other than `echo`.

## 6. Revision history

| Date | By | From → To | Notes |
|------|----|-----------|-------|
| 2026-07-30 | AI | — → proposed | R0 implementation plan drafted for approval |
| 2026-07-30 | @akiocox | proposed → approved | Approved; `SPEC-AIP-002` → `plan-approved`, code authorised |
| 2026-07-30 | AI | approved → done | S1–S9 landed on `r0-initial-spec` (not the §0 branch name — the spec branch already carried SPEC/PLAN, so R0 ships as one PR from there). Deviations from the spec's §3 are recorded in `SPEC-AIP-002` §5. |
| 2026-07-31 | @akiocox | done | Retroactively approved and merged to `main` (PR #1). Deviations recorded in `SPEC-AIP-002` §5. |
