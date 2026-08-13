"""The empty file (SPEC-NSP-001 AC-1).

One line, and it runs the whole template on defaults:

    navigator-orchestrator run examples/minimal.py \
        --question "what is the refund window?" --dir ./docs

Every hook is an optional override, so a workflow file is a diff against a
template rather than a program. This is the floor the interface has to clear —
if the smallest useful workflow needs more than this, the defaults are wrong.
"""

WORKFLOW = "doc-qa"
