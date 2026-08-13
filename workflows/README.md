# workflows/

Workflow **definitions** live here. Engine code lives in
`src/navigator_orchestrator/` and never reads this directory.

That separation is not a convention anyone has to remember — it is enforced by
tests. `tests/test_example_workflow_is_removable.py` fails the build if anything
under `src/`, `tests/`, `features/` or `.github/` learns the name of a workflow
in here, and `tests/test_sdk_isolation.py` AST-scans imports to keep the platform
from ever reaching into the authoring SDK.

## Starting your own

```bash
rm -rf workflows/kyc          # the sample is one directory; deleting it breaks nothing
mkdir -p workflows/<yours>/{flows,judges,prompts}
cp <somewhere>/navigator-orchestrator.toml workflows/<yours>/
```

A workflow project is **a directory containing `navigator-orchestrator.toml`**.
It is not a Python package: there is no `pyproject.toml`, no build backend, no
install step, and no reinstall after an edit. The CLI walks up from the working
directory to find the manifest, the way `git` finds `.git`.

Adding a judge means adding a file. That is the whole extension model.

## Schemas at the edges, free text inside

A workflow has exactly two typed surfaces, and they are both edges:

```python
Template(
    name="kyc-onboarding",
    params=(Param("client_id", doc="Client to screen"),),   # input edge
    publishes="outcome",                                     # which key is the result
    result_schema="onboarding-outcome",                      # its shape, from [schemas.*]
    steps=(...),
)
```

`params` is what an operator types to launch a run, so typing it lets a console
render a form and catches a typo before anything executes. `publishes` and
`result_schema` describe the record that comes out, so a console can render a
table rather than a JSON dump. Both are optional; a bare string in `params` is
shorthand for a required string.

**Everything between the steps stays untyped, and should.** The pool keys one
step hands the next are `Any`. Rigid structure between agentic nodes is
self-defeating: the useful outputs are frequently the ones no schema
anticipated. `tests/test_param_schema.py` fails the build if a schema field ever
appears on the general step contract, so this is enforced rather than
remembered.

## The three kinds of thing in a definition

**1. Declarative steps** — the default. A step names a generic engine executor
(`data`, `lookup`, `rules`, `validate`, `gate`, `service`) and its inputs and
outputs. No Python. Reviewable as a diff.

**2. Agent configurations** — a YAML file per agent: model, inputs, temperature,
failure mode and prompt. Declared, not coded, so changing what an agent is shown
is a config change rather than a deployment.

**3. Injected Python, when neither of the above fits** — see below.

## Injecting Python steps

When no generic engine step does what you need, write a plain function in a
workflow `.py` file. Module-level functions are matched **by name** against the
template's step names:

```python
WORKFLOW = "kyc-onboarding"


def resolve_sanctions_jurisdiction(ctx, client, business_interests):
    """Overrides the step of the same name. Ordinary Python, ordinary testing."""
    controlling = next((i for i in business_interests if i["controlling_stake"]), None)
    if controlling:
        return controlling["country_of_operations"]
    return client["address_detail"].get("country_of_legal_residency")
```

Three properties make this safe to hand to another team:

- **Nothing is registered and nothing runs at import.** The loader reads the file
  by inspection — it is data about behaviour. Definition order is irrelevant and
  there is no base class to inherit or decorator to remember.
- **Every hook is an optional override**, so a workflow file is a *diff against a
  template* rather than a program. The smallest useful workflow is one line.
- **Functions are pure `(ctx, ...) -> value`.** Side effects go through `ctx`,
  which is injected. `tests/test_purity.py` enforces this: a step that builds a
  client at import time or writes into shared state fails the build.

For an implementation shared across several workflows, register it once under a
`namespace.verb` name and reference it with `uses=`:

```python
register_implementation("screening.normalise_name", normalise_name)
# then in the template:  Step("normalise", "agent", uses="screening.normalise_name")
```

## Why the platform never imports any of this

The API process must never load, bind or execute definition code. If that
inverts, arbitrary user Python becomes reachable from a multi-tenant service —
the one constraint the design says cannot be relaxed later. The SDK may import
engine contracts; the platform may not import the SDK. That direction is
asserted, not assumed.

## The sample

`kyc/` is a KYC client-onboarding workflow — adverse media, PEP, sanctions and
eligibility tiering. It is deliberately **eight deterministic steps, two agents
and two human gates**, because that ratio is the argument: rules where the answer
is defined, agents where it is genuinely open, humans where being wrong is
expensive.

Read `kyc/DESIGN.md` for why each step is the kind it is. It is a worked example
of the reasoning, and it is the part worth keeping even after you delete the
workflow itself.

> The sample is design, not running code: its YAML has not been validated against
> `sdk/loader.py`. Treat it as a shape to copy, not a thing to run.
