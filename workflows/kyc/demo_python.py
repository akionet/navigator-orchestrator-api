"""Screen clients from Python.

    uv run --project ../.. python demo_python.py CL-0001
    uv run --project ../.. python demo_python.py CL-0001 CL-0004 CL-0008
    uv run --project ../.. python demo_python.py --file ids.example.txt

The whole API is `run_batch` plus `outcomes_by_status`. A pause and a decline
are ordinary outcomes, so nothing here needs a `try/except`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from navigator_orchestrator import ids_from_file, outcomes_by_status, run_batch

HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    if argv[:1] == ["--file"]:
        params = ids_from_file(HERE / argv[1])
    else:
        params = [{"client_id": client_id} for client_id in argv or ["CL-0001"]]

    outcomes = run_batch("kyc-onboarding", params, project_dir=HERE)

    for outcome in outcomes:
        detail = outcome.reason or (outcome.gate or {}).get("step", "")
        print(f"{outcome.params['client_id']}  {outcome.status:<10} {detail}")
        if outcome.ok:
            print(f"    tier: {outcome.output['tier']}")

    print()
    print({status: len(group) for status, group in outcomes_by_status(outcomes).items()})

    # A decline is a correct answer, so only a genuine fault is a non-zero exit.
    return 1 if any(outcome.status == "failed" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
