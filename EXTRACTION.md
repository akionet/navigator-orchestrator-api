# Extraction record

`navigator-orchestrator` was extracted from a private codebase in which the same
engine ran a proprietary content-editorial scenario. This file records what was
removed, what was renamed, and what is knowingly incomplete — so a reviewer can
check the scrub rather than trust it.

Delete this file once the project has its own history and the gaps below are
closed.

## Extracted as a fresh history, deliberately

This is **not** a fork and shares no commits with its origin. The upstream
history contains, in a checked-in manifest, a written description of a live
production authentication weakness — that a JWT signing key was absent from the
secret store, that the service consequently fail-closed, and that a fallback
credential was ordered first to work around it. Rewriting history with
`filter-repo` would have been slower and less certain than starting clean, and a
public repository whose history contains that description is not something a
rewrite reliably fixes once it has been cloned.

## Removed entirely

Nothing below is present in any file or in history.

| Removed | Why |
|---|---|
| The project manifest, which carried three credential environment-variable names, a secret-store path and private hostnames | credential names and an operational security disclosure |
| The whole editorial scenario — flows, judges, rubrics, fixtures, runbooks | proprietary product content |
| `docs/specs/**`, `docs/guides/**`, `docs/design/**` | reference the private product, its schema and its API surface throughout |
| `docs/ROADMAP.md` | the private product's roadmap, not engine documentation |
| The scenario workflows and their prompts | product-specific |
| `CLAUDE.md`, internal review notes | internal working documents |

## Renamed

| From | To |
|---|---|
| Python package | `navigator_orchestrator` |
| Environment prefix | `NAVIGATOR_*` |
| Project manifest | `navigator-orchestrator.toml` |
| Repositories | `navigator-orchestrator-api`, `navigator-orchestrator-app` |

Scenario vocabulary in SDK examples and fixtures was neutralised — example
backends are now `client-service` and `billing`, example endpoints `/v1/records`
and `/v1/submissions`, example hooks `client.draft`, and the example judge id is
`sanctions`. These are illustrative strings in docstrings and tests, never live
configuration.

## Verification

Before any publish, grep both repositories for the originating organisation and
product names, the three credential variable names, and the old Python package
and environment prefixes. The denylist is deliberately **not** written down here:
committing the exact strings to a public repository is the thing this file exists
to prevent, and it would leave a searchable record of an internal credential name.

Keep the pattern in an untracked local file:

```bash
grep -rIn -E -f ../scrub-denylist.txt . \
  --exclude-dir=.git --exclude-dir=.venv --exclude-dir=node_modules
```

At the time of the initial commit this matched nothing in either repository,
with the sole exception of this file before it was redacted.

## Known gaps

**Two SDK test files were dropped rather than ported.** `test_event_log.py` and
`test_graph_runner.py` exercised the event log and the graph runner *through* the
old scenario's template, and their fixtures depended on its specifics — a
blocked-run case worked by feeding butter to a judge that rejected non-vegan
ingredients. Re-pointing them needs a neutral built-in template with an
equivalent step shape, which is real work rather than a rename.

They were dropped rather than quietly deleted or faked: the SDK's event log and
graph runner are **genuinely less covered here than upstream**, and that should
be closed before the SDK is described as production-ready. Everything else in
`sdk/` retains its tests.

**The KYC reference workflow is design, not running code.** `workflows/kyc/`
holds a flow definition, two agent configurations and synthetic fixtures. The
YAML has not been validated against `sdk/loader.py`; expect field-name drift on
first load. See `docs/DESIGN-KYC-001-client-onboarding.md`, which also lists the
open questions — notably an ambiguity in the Premium eligibility rule that needs
a decision before it is implemented.

**Licence not chosen.** `LICENSE` was carried across unchanged and must be
reviewed before publication.

## State at the initial commit

| | API | App |
|---|---|---|
| `ruff check` | ✅ | — |
| `ruff format --check` | ✅ 107 files | — |
| `mypy` | ✅ 56 source files | — |
| `pytest` | ✅ **428 passed, 4 skipped** | — |
| `tsc --noEmit` | — | ✅ |
| `eslint` | — | ✅ |
| unit tests | — | ✅ **27 passed** |
| browser BDD | — | ✅ **8 scenarios, 55 steps** |

The real-HTTP journey (`@F-WFB-R2-06`) is excluded from that BDD run, as it is
upstream: it needs both processes running together and is gated behind a
cross-repository token there. With both repositories in the open that gate can
simply go away, and the journey should be turned back on.
