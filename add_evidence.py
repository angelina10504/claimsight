"""
Send in evidence for a claim that ClaimSight asked about, then let it carry on.

A claim stops when something it needs is missing. This records what arrives,
checks it with plain rules, and can resume the claim straight away.

Run:  uv run python add_evidence.py CLM-9002
          what is still needed, and what has been sent so far

      uv run python add_evidence.py CLM-9002 --item "invoice total" --value 42869
      uv run python add_evidence.py CLM-9002 --item "windshield photograph" --file ~/photo.jpg
          record one piece of evidence

      uv run python add_evidence.py CLM-9002 --resume
          carry on the claim with everything accepted so far

      uv run python add_evidence.py CLM-9002 --item "invoice total" --value 42869 --resume
          both, in one go

Nothing here asks a model anything: whether evidence is acceptable decides
whether someone gets paid, so it is a rule you can read.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import claim_flow   # noqa: E402
import evidence     # noqa: E402


def read_args(argv: list) -> dict:
    """Parse --item, --value, --file and the flags. Kept simple on purpose."""
    out = {"claim_id": "", "item": None, "value": None, "file": None, "note": "",
           "resume": "--resume" in argv, "no_input": "--no-input" in argv,
           "verbose": "--verbose" in argv}
    for n, arg in enumerate(argv):
        if arg.startswith("CLM-"):
            out["claim_id"] = arg
        elif arg in {"--item", "--value", "--file", "--note"} and n + 1 < len(argv):
            out[arg.lstrip("-")] = argv[n + 1]
    return out


def show(claim_id: str) -> None:
    state = claim_flow.saved_state(claim_id)
    if not state:
        print(f"{claim_id} has not been through the Flow yet.")
        print(f"Run it first: uv run python run_flow.py {claim_id}")
        return

    print(f"\nCLAIM {claim_id}   status: {state.get('status') or 'unknown'}")
    asked = evidence.outstanding(state)
    if asked:
        print("\nStill needed:")
        for request in asked:
            field = evidence.field_for(request.item)
            print(f"  - {request.item}   (from the {request.from_whom})")
            print(f"      because {request.why}")
            if request.from_whom == "claimant":
                flag = "--value <value>" if field else "--file <path>"
                print(f"      send it: uv run python add_evidence.py {claim_id} "
                      f'--item "{request.item}" {flag}')
    else:
        print("\nNothing outstanding.")

    sent = evidence.summary(claim_id)
    if sent:
        print("\nEvidence on file:")
        for line in sent:
            print(f"  {line}")
    pending = evidence.unapplied(claim_id)
    if pending:
        print(f"\n{len(pending)} accepted piece(s) not yet acted on. To carry on:")
        print(f"  uv run python add_evidence.py {claim_id} --resume")


def main() -> None:
    args = read_args(sys.argv[1:])
    if not args["claim_id"]:
        sys.exit(__doc__)
    claim_id = args["claim_id"]

    if args["item"]:
        if args["value"] is None and args["file"] is None:
            sys.exit("Give --value <value> or --file <path> with --item.")
        state = claim_flow.saved_state(claim_id)
        entry = evidence.submit(claim_id, args["item"], value=args["value"],
                                file=args["file"], note=args["note"], state=state or None)
        mark = "ACCEPTED" if entry["accepted"] else "NOT ACCEPTED"
        print(f"\n{mark}: {entry['item']}")
        print(f"  {entry['verdict']}")
        if entry.get("file"):
            print(f"  kept at {entry['file']}")
        if not entry["accepted"]:
            sys.exit(1)

    if args["resume"]:
        outcome = claim_flow.resume_claim(claim_id, interactive=not args["no_input"],
                                          verbose=args["verbose"])
        if not outcome["resumed"]:
            print(f"\nNot resumed: {outcome['why']}")
            sys.exit(1)
        state = outcome["state"]
        print(f"\n{claim_id}: {state['status']}   route {state['route']}   "
              f"payout Rs {state['final_payout']:,.2f}")
        print(f"full explanation: uv run python show_report.py {claim_id}")
        return

    if not args["item"]:
        show(claim_id)
        return

    print(f"\nTo carry on the claim: uv run python add_evidence.py {claim_id} --resume")


if __name__ == "__main__":
    main()
