"""
Run the settlement calculator against every generated claim and report
agreement with the ground truth.

Run:  uv run python test_settlement.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src" / "tools"))
from settlement import calculate_settlement, classify_material, explain  # noqa: E402

ROOT = Path(__file__).parent
CLAIMS = ROOT / "data" / "claims"
POLICIES = ROOT / "data" / "policies"

TOLERANCE = 1.0          # rupees


def main():
    claim_files = sorted(CLAIMS.glob("CLM-*.json"))
    if not claim_files:
        sys.exit("No claims found. Run generate_data.py first.")

    passed, failed, misclassified = 0, [], []

    for path in claim_files:
        claim = json.loads(path.read_text())
        policy = json.loads((POLICIES / f"{claim['policy_no']}.json").read_text())
        truth = claim["ground_truth"]

        if not truth["is_covered"]:
            continue                       # rejection is the router's job

        result = calculate_settlement(claim["line_items"], policy)
        diff = abs(result.payout - truth["expected_payout"])

        if diff <= TOLERANCE:
            passed += 1
        else:
            failed.append((claim["claim_id"], truth["expected_payout"],
                           result.payout, diff))

        # separately: does description-only classification match the label?
        for item in claim["line_items"]:
            guessed = classify_material(item["description"])
            if guessed != item["material"]:
                misclassified.append(
                    (claim["claim_id"], item["description"],
                     item["material"], guessed))

    total = passed + len(failed)
    print(f"Settlement agreement: {passed}/{total} claims within Rs {TOLERANCE}")

    if failed:
        print("\nMismatches:")
        for cid, expected, got, diff in failed:
            print(f"  {cid}: expected {expected:,.2f}, got {got:,.2f} "
                  f"(off by {diff:,.2f})")

    print(f"\nMaterial classification from description alone: "
          f"{len(misclassified)} mismatches")
    for cid, desc, truth_mat, guess in misclassified[:10]:
        print(f"  {cid}: '{desc}' labelled {truth_mat}, classified {guess}")

    # show one full breakdown so the output is inspectable
    sample = json.loads(claim_files[0].read_text())
    sample_policy = json.loads(
        (POLICIES / f"{sample['policy_no']}.json").read_text())
    print(f"\n--- {sample['claim_id']} breakdown "
          f"(zero_dep={sample_policy['zero_depreciation']}, "
          f"age={sample_policy['vehicle_age_years']}) ---")
    print(explain(calculate_settlement(sample["line_items"], sample_policy)))

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
