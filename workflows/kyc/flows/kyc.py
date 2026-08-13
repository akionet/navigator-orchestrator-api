"""The KYC workflow file.

One mandatory line. Every step in `kyc-onboarding` ships a working default, so
this runs the whole screening as-is:

    nav run flows/kyc.py --client_id CL-0001

A hook is an *optional override*, matched to a step **by name**. Nothing is
registered and nothing runs at import — this file is data about behaviour, read
by inspection. Definition order is irrelevant.

To change one step, write a function called after it. For example, to widen
sanctions matching from exact to fuzzy without touching the template:

    def sanctions_check(ctx, client, client_id):
        ...              # your own screening, same name, same pool keys

`nav check flows/kyc.py` validates the file against the template before any run,
so a function named after a step that does not exist is caught immediately
rather than being silently ignored.
"""

WORKFLOW = "kyc-onboarding"
