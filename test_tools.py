"""
Run the Assessment tools on every claim, with no LLM involved, and report
which fraud patterns the deterministic checks catch on their own.

This shows the division of labour: tools catch what is checkable by rule,
and whatever is left is exactly what the agents must judge.

Run:  uv run python test_tools.py            (dev split, 20 claims)
      uv run python test_tools.py --all      (all 60 claims)
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src" / "tools"))

from assessment_tools import (  # noqa: E402
    _part_name, check_part_prices, check_policy_period, find_prior_claims,
)

CLAIMS = ROOT / "data" / "claims"

# Which fraud patterns a rule can catch, and which need an agent instead
NEEDS_AGENT = {
    "phantom_part": "Damage Analyst: compare billed parts with the photos (Phase-3)",
    "excluded_wear_tear": "Policy Analyst: recognise wear and tear as an exclusion",
}


def run_checks(claim: dict) -> dict:
    """What an Assessment crew would see: the invoice lines and dates, no labels."""
    lines = [{"description": i["description"], "amount": i["amount"]} for i in claim["line_items"]]
    parts = sorted({_part_name(i["description"]) for i in claim["line_items"]} - {None})
    return {
        "period": check_policy_period(claim["policy_no"], claim["incident_date"]),
        "prices": check_part_prices(lines),
        "prior": find_prior_claims(claim["policy_no"], claim["claim_id"], claim["incident_date"], parts),
    }


def main() -> None:
    use_all = "--all" in sys.argv
    files = sorted(CLAIMS.glob("CLM-*.json"))
    claims = [json.loads(f.read_text()) for f in files]
    if not use_all:
        claims = [c for c in claims if c["split"] == "dev"]
    if not claims:
        sys.exit("No claims found. Run generate_data.py first.")

    by_fault = defaultdict(lambda: {"n": 0, "caught": 0, "how": set()})
    print(f"{'claim':<10} {'fault':<20} {'period':<8} {'prices':<8} {'prior':<8} flagged by")
    print("-" * 78)

    for claim in claims:
        fault = claim["ground_truth"]["fault_type"]
        r = run_checks(claim)
        hits = []
        if not r["period"]["within_period"]:
            hits.append("policy period")
        if r["prices"]["suspicious"]:
            hits.append("price check")
        if r["prior"]["possible_duplicate"]:
            hits.append("prior claims")

        row = by_fault[fault]
        row["n"] += 1
        if hits:
            row["caught"] += 1
            row["how"].update(hits)

        print(f"{claim['claim_id']:<10} {fault:<20} "
              f"{'ok' if r['period']['within_period'] else 'OUTSIDE':<8} "
              f"{'FLAG' if r['prices']['suspicious'] else 'ok':<8} "
              f"{'DUP' if r['prior']['possible_duplicate'] else 'ok':<8} "
              f"{', '.join(hits) or '-'}")

    print("\nSUMMARY  " + ("(all claims)" if use_all else "(dev split)"))
    print("-" * 78)
    for fault, row in sorted(by_fault.items()):
        if fault == "clean":
            false_alarms = row["caught"]
            print(f"{'clean':<20} {row['n']:>2} claims, {false_alarms} false alarm(s)"
                  + (f"  [{', '.join(sorted(row['how']))}]" if false_alarms else ""))
            continue
        caught = f"{row['caught']}/{row['n']} caught by tools"
        how = f"  [{', '.join(sorted(row['how']))}]" if row["how"] else ""
        note = f"  -> {NEEDS_AGENT[fault]}" if fault in NEEDS_AGENT else ""
        print(f"{fault:<20} {caught}{how}{note}")

    # One worked example of each tool's output, for the report
    example = next((c for c in claims if c["ground_truth"]["fault_type"] == "inflated_amount"), None)
    if example:
        print(f"\n--- price check on {example['claim_id']} (inflated_amount) ---")
        for flag in run_checks(example)["prices"]["flags"]:
            print(f"  {flag}")
    example = next((c for c in claims if c["ground_truth"]["fault_type"] == "duplicate_claim"), None)
    if example:
        print(f"\n--- prior claims on {example['claim_id']} (duplicate_claim) ---")
        print(f"  {run_checks(example)['prior']['note']}")


if __name__ == "__main__":
    main()
