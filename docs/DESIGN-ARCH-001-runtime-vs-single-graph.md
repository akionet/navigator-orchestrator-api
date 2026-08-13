# DESIGN-ARCH-001 — A workflow runtime, or one graph with more tools?

Two ways to grow past a single LangGraph with a tool chain and an agentic loop:

- **A. Deepen the graph.** Keep one agent, source its tools over MCP, and hold
  the tool configuration in a database. Capability grows by adding tools.
- **B. Add a runtime above the graph.** Many declared workflows, each its own
  graph, with typed edges, human gates, durable runs and an audit trail.
  Capability grows by adding workflows.

They are usually argued as alternatives. They are not: **MCP operates inside a
step, and the runtime operates around it.** The interesting question is not
which one, but what each is load-bearing for.

## 1. The layers

```
┌──────────────────────────────────────────────────────────────────────────┐
│  OPERATOR SURFACE            console · CLI · Python API                  │
│  who launches, who decides, who can see the audit chain                  │
└───────────────┬──────────────────────────────────────────────────────────┘
                │
┌───────────────▼──────────────────────────────────────────────────────────┐
│  NAVIGATOR RUNTIME                                          ← layer B    │
│                                                                          │
│   definitions          runs                    gates                     │
│   ┌──────────────┐    ┌──────────────────┐    ┌────────────────────┐     │
│   │ versioned    │    │ queued → running │    │ pause · payload    │     │
│   │ documents    │    │ → awaiting →     │    │ decision · resume  │     │
│   │ (Postgres)   │    │ completed        │    │ by a different     │     │
│   │              │    │ declined/failed  │    │ actor, later       │     │
│   └──────────────┘    └──────────────────┘    └────────────────────┘     │
│                                                                          │
│   answers: which workflows exist · did the sanctions step run ·          │
│            who approved · what happens when the process dies             │
└───────────────┬──────────────────────────────────────────────────────────┘
                │  one graph per workflow
┌───────────────▼──────────────────────────────────────────────────────────┐
│  LANGGRAPH                                                               │
│   load → validate → [agent] → [agent] → check → ⏸ gate → rules → emit    │
│                        │                                                 │
└────────────────────────┼─────────────────────────────────────────────────┘
                         │  inside one agent step
┌────────────────────────▼─────────────────────────────────────────────────┐
│  AGENTIC LOOP + TOOLS                                       ← layer A    │
│   reason → call tool → observe → repeat                                  │
│   tools discovered over MCP, configuration in a database                 │
│                                                                          │
│   answers: what can this step reach · how does it decide what to call    │
└──────────────────────────────────────────────────────────────────────────┘
```

The two layers answer different questions, which is why an argument between
them tends not to resolve.

## 2. What each is load-bearing for

**A single agentic loop has non-deterministic control flow.** The model decides
what happens next. That is the point of it, and it is exactly right when the
correct sequence is not known ahead of time — research, triage, open-ended
question answering.

**A runtime has declared control flow with agentic steps inside it.** The
topology is data; only the reasoning is open. That matters when someone must
answer *"was this control applied?"* without inspecting a transcript.

The KYC reference workflow is the sharp case. "Did we screen this client against
the sanctions list?" must be answerable from the definition. In layer A the
honest answer is "the model had the tool available and usually calls it" — which
is not an answer a compliance function can use. In layer B the step either exists
in the definition or it does not, and the run record says whether it ran.

Note what this does *not* claim: the agentic steps are still non-deterministic
and should be. `adjudicate_media` decides whether a defence solicitor is
implicated in their client's fraud, and no rule engine is going to do that. The
runtime constrains *when* judgement is applied, not the judgement.

## 3. Pro and con

| | **A. One graph, MCP tools, DB config** | **B. Runtime over many graphs** |
|---|---|---|
| **Control flow** | Model chooses. Flexible; not reproducible | Declared and reviewable as a diff |
| **"Did the control run?"** | Inferred from a transcript | Answered from the definition and run record |
| **Adding capability** | Add a tool — genuinely config | Add a workflow — more ceremony |
| **Human approval** | Ad hoc; usually in the surrounding app | First class: pause, durable payload, different actor, audit chain |
| **Process death** | Loop restarts or the work is lost | Run resumes from a checkpoint, possibly on another host |
| **Reproducibility** | Same input, different path | Same input, same topology; only reasoning varies |
| **Cost of a wrong turn** | Cheap — try again | Higher — a gate may already have been decided |
| **Time to first value** | Hours | Days |
| **Failure domain** | One loop, one blast radius | Per-step, with per-branch outcomes on fan-out |
| **Skills needed** | Prompt and tool design | Prompt design *and* workflow modelling |
| **Where it strains** | Long, multi-party, audited processes | Short, exploratory, one-shot tasks |
| **Operational surface** | One service | Runtime, worker, stores, definitions |

The last row is the honest cost. Layer B is more moving parts, and if the work is
genuinely one agent answering one question, it is overhead with a rationale
attached.

## 4. Versioned definitions in Postgres

The strongest business argument, and worth stating precisely because it is easy
to overstate.

**What it buys.** A workflow definition becomes a versioned document rather than
a file in a release. Then:

- A rule change is a **document version**, not a deployment.
- *"What rules were in force on 3 March?"* is a query, not archaeology.
- A run records **which version it used**, so an outcome is reproducible against
  the definition that produced it — not the one currently deployed.
- Rollback is selecting a prior version.
- Review is a diff a non-engineer can read.

That is real decoupling from the SDLC release cycle, and for a compliance
workflow whose thresholds move with policy rather than with code, it is probably
the single largest benefit on offer.

**What it does not buy, and this bounds the claim.** An externalised definition
still names Python: `uses: kyc.sanctions_check` resolves to a registered
function in a deployed worker. So versioning decouples **orchestration** from
release, not **behaviour**. Changing the eligibility *thresholds* becomes a
document edit; changing how sanctions matching *works* is still a deployment.

Two consequences worth designing for rather than discovering:

1. **Compatibility.** A definition version referencing a `uses` name no deployed
   worker has is broken at load. Definitions and workers need a compatibility
   check, and a version should record the implementations it depends on.
2. **Approval.** Removing definitions from code review removes the review. If a
   document version can change a compliance threshold without an engineer, it
   needs its own approval gate — which the runtime already knows how to do, and
   is a pleasing recursion: the workflow that approves workflow changes.

## 5. Recommendation

**Both, layered — which is your position, and I agree with it.** MCP inside
agent steps, the runtime around them, definitions versioned in Postgres. They
are not competing designs; they are different altitudes.

Where I would differ is **sequence**, because the ordering carries risk that the
"not mutually exclusive" framing hides:

1. **The YAML/JSON parser first.** Versioned documents are pointless until a
   document can be loaded at all. Today definitions are Python; there is nothing
   to version. This is also the cheapest step — `uses` and `when` are already
   strings precisely so this is a parser, not a redesign.
2. **Then the worker and the console.** Definitions that only the CLI can run
   are not a platform other teams adopt.
3. **Then Postgres-backed versioning,** with the compatibility check and the
   approval gate from §4 designed in from the start rather than retrofitted.
   Retrofitting approval onto a system where documents already move freely means
   taking away a capability people have started using.
4. **MCP inside agent steps whenever it is useful** — it is orthogonal to all
   three and needs no coordination with them.

The one thing I would push back on: adopting layer B *because* it is more
capable is the wrong reason. Adopt it where the work is multi-party, audited, or
long-running enough that a process restart matters. For a step that is genuinely
one agent with good tools, layer A inside a single-step workflow is a legitimate
answer, and the runtime should not make that feel like a failure.

## 6. Status

`proposed`. No decision recorded. Written to compare two positions rather than
to authorise either, and the sequencing in §5 is the part most worth arguing
with.
