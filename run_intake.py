"""
Run the Intake crew on a single claim and score the extraction.

Run:  uv run python run_intake.py CLM-5000
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from config import describe                       # noqa: E402
from crews.intake_crew import run_intake          # noqa: E402

CLAIMS = ROOT / "data" / "claims"


def score(claim_id: str, intake) -> None:
    truth = json.loads((CLAIMS / f"{claim_id}.json").read_text())
    gt_items = truth["line_items"]
    got_items = intake.invoice.line_items

    print("\n" + "=" * 60)
    print(f"EXTRACTION SCORE  {claim_id}   ({describe()})")
    print("=" * 60)

    print(f"line items   expected {len(gt_items)}, got {len(got_items)}")

    gt_total = truth["invoice_total"]
    got_total = intake.invoice.total
    if got_total is None:
        print(f"total        expected {gt_total:,}, got NOTHING")
    else:
        delta = abs(got_total - gt_total)
        verdict = "match" if delta < 1 else f"off by {delta:,.2f}"
        print(f"total        expected {gt_total:,}, got {got_total:,.0f}  [{verdict}]")

    gt_amounts = sorted(i["amount"] for i in gt_items)
    got_amounts = sorted(i.amount for i in got_items)
    print(f"amounts      {'match' if gt_amounts == got_amounts else 'MISMATCH'}")
    if gt_amounts != got_amounts:
        print(f"  expected {gt_amounts}")
        print(f"  got      {got_amounts}")

    gt_materials = [i["material"] for i in gt_items]
    got_materials = [i.material for i in got_items]
    if len(gt_materials) == len(got_materials):
        hits = sum(a == b for a, b in zip(gt_materials, got_materials))
        print(f"materials    {hits}/{len(gt_materials)} correct")

    gt_parts = set(truth["ground_truth"]["damaged_parts"])
    got_parts = {p.lower().strip() for p in intake.damaged_parts}
    print(f"parts        expected {sorted(gt_parts)}")
    print(f"             got      {sorted(got_parts)}")

    print(f"complete     {intake.is_complete}   missing={intake.missing_fields}")
    if intake.notes:
        print(f"notes        {intake.notes}")


def main() -> None:
    claim_id = sys.argv[1] if len(sys.argv) > 1 else "CLM-5000"
    if not (CLAIMS / f"{claim_id}.json").exists():
        sys.exit(f"No such claim: {claim_id}. Run generate_data.py first.")

    print(f"Running intake on {claim_id} using {describe()}\n")
    intake = run_intake(claim_id)

    if intake is None:
        sys.exit("Crew returned no structured output - the model failed to "
                 "produce a valid ClaimIntake.")

    score(claim_id, intake)

    out = ROOT / "data" / "intake"
    out.mkdir(exist_ok=True)
    (out / f"{claim_id}.json").write_text(intake.model_dump_json(indent=2))
    print(f"\nSaved -> data/intake/{claim_id}.json")


if __name__ == "__main__":
    main()
