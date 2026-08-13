.DEFAULT_GOAL := help
.PHONY: help install lint fmt typecheck test test-live purity check clean run \
        requirements requirements-uat requirements-golive

UV ?= uv

# Which provider extra to bake into a generated requirements.txt.
# UAT is Gemini on Vertex; golive is Claude on Bedrock.
EXTRA ?= vertex

help:               ## Show these targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:            ## Spine + dev tools (add --extra vertex for a real model)
	$(UV) sync --group dev

lint:               ## ruff check + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:                ## Apply formatting and safe fixes
	$(UV) run ruff check . --fix
	$(UV) run ruff format .

typecheck:          ## mypy --strict over src/navigator_orchestrator
	$(UV) run mypy

test:               ## Unit + BDD, hermetic (FakeClient, $0 tokens)
	$(UV) run pytest -q

purity:             ## AC-3 node purity gate on its own
	$(UV) run pytest tests/test_purity.py -q

test-live:          ## The one opt-in provider smoke
	LIVE_LLM=true $(UV) run pytest -m live -q

check: lint typecheck test   ## What CI runs

# --------------------------------------------------------------------------
# requirements.txt
#
# The source of truth for dependencies is `pyproject.toml` (what we depend on)
# plus `uv.lock` (exactly which versions, transitively). A requirements.txt is
# a *generated artefact* for deploy targets that insist on one — Cloud Run
# source deploys and buildpacks being the reason it exists here.
#
# Never hand-edit the generated file: regenerate it. Treat it as build output,
# like a compiled binary. `make requirements` after any dependency change.
#
#   make requirements                 # default extra (vertex / UAT)
#   make requirements EXTRA=bedrock   # golive profile
#   make requirements-uat             # explicit UAT profile
#   make requirements-golive          # explicit golive profile
#
# Flags, and why each one is there:
#   --no-dev            runtime only; ruff/mypy/pytest have no business in a
#                       deployed image
#   --no-emit-project   drop the `-e .` self-reference, which a buildpack
#                       cannot install
#   --no-hashes         buildpacks reject hashes unless *every* package has
#                       one; drop `--no-hashes` if your target verifies them
#   --extra <name>      bake in exactly one provider adapter, so a UAT image
#                       does not ship the Bedrock SDK and vice versa
# --------------------------------------------------------------------------

requirements:       ## Generate requirements.txt from uv.lock (EXTRA=vertex|bedrock|anthropic)
	$(UV) export --no-dev --no-emit-project --no-hashes --extra $(EXTRA) \
	  --format requirements-txt -o requirements.txt
	@echo "wrote requirements.txt (extra: $(EXTRA)) — generated, do not hand-edit"

requirements-uat:   ## requirements.txt for UAT (Gemini on Vertex)
	@$(MAKE) requirements EXTRA=vertex

requirements-golive: ## requirements.txt for golive (Claude on Bedrock)
	@$(MAKE) requirements EXTRA=bedrock

run:                ## Serve locally (fake model unless NAVIGATOR_MODEL is set)
	$(UV) run --extra server uvicorn navigator_orchestrator.api.app:app --reload

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build
