"""The KYC workflow, run from its YAML definition.

    navigator-orchestrator run flows/kyc_doc.py --client_id CL-0001

Identical behaviour to `kyc.py`; the difference is where the pipeline is
declared. `kyc.py` names a template built in Python, this one names a template
parsed from `templates/kyc_onboarding.yaml`.

Nothing else changes: hooks still override by name, gates still pause, the run
store and the audit chain are the same. That is the point — externalising the
definition must not fork the behaviour.
"""

WORKFLOW = "kyc-onboarding-doc"
