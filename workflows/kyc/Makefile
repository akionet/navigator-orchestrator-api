# KYC operator commands.
#
# The CLI is general; this file names the verbs *this* workflow has. An operator
# types `make queue` and `make screen client=CL-0004`, not a path to a flow file
# and a run id they had to copy from a previous command.
#
# Copy this into your own workflow project and rename the verbs. It is the
# cheapest part of the whole design to change.

NAV  := uv run --project ../.. navigator-orchestrator
FLOW := flows/kyc.py

# Offline and free unless you deliberately override it.
export NAVIGATOR_MODEL ?= fake:local

.DEFAULT_GOAL := help
.PHONY: help check screen batch queue runs show approve reject revise clean

help:               ## Show these commands
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

check:              ## Validate the workflow file without running anything
	@$(NAV) check $(FLOW)

screen:             ## Screen one client: make screen client=CL-0001
	@test -n "$(client)" || (echo "usage: make screen client=CL-0001"; exit 2)
	@$(NAV) run $(FLOW) --client_id $(client)

batch:              ## Screen several: make batch clients="CL-0001 CL-0004" | file=ids.txt
	@test -n "$(clients)$(file)" || (echo 'usage: make batch clients="CL-0001 CL-0004" or file=ids.txt'; exit 2)
	@for id in $(clients) $$(test -n "$(file)" && grep -v '^\s*\#' $(file) | tr -d '\r'); do \
		echo "── $$id ──"; \
		$(NAV) run $(FLOW) --client_id $$id || true; \
	done

queue:              ## What is waiting for a human decision
	@$(NAV) runs | awk 'NR==1 || /awaiting_human/'

runs:               ## Every run, most recent last
	@$(NAV) runs

show:               ## The durable payload a reviewer sees: make show run=<id>
	@test -n "$(run)" || (echo "usage: make show run=<id>  (see: make queue)"; exit 2)
	@$(NAV) show $(run)

approve:            ## Approve a paused run: make approve run=<id> [why="..."]
	@test -n "$(run)" || (echo 'usage: make approve run=<id> [why="cleared"]'; exit 2)
	@$(NAV) decide $(FLOW) $(run) --approve $(if $(why),--comment "$(why)",)

reject:             ## Reject a paused run: make reject run=<id> [why="..."]
	@test -n "$(run)" || (echo 'usage: make reject run=<id> [why="not satisfied"]'; exit 2)
	@$(NAV) decide $(FLOW) $(run) --reject $(if $(why),--comment "$(why)",)

revise:             ## Send back for revision: make revise run=<id> [why="..."]
	@test -n "$(run)" || (echo 'usage: make revise run=<id> [why="need provenance"]'; exit 2)
	@$(NAV) decide $(FLOW) $(run) --revise $(if $(why),--comment "$(why)",)

# Both the run log and the checkpoints live here. Removing only the logs would
# leave paused runs resumable with no record of them, which is worse than either.
clean:              ## Discard local run history and checkpoints
	@rm -rf .navigator-orchestrator && echo "run history cleared"
