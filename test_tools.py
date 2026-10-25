"""
Run the Assessment tools on every claim, with no LLM involved, and report
which fraud patterns the deterministic checks catch on their own.

This shows the division of labour: tools catch what is checkable by rule,
and whatever is left is exactly what the agents must judge.

Which claims are used comes from data/manifest.json, the official 60, never
from whatever happens to be in data/claims. Demonstration fixtures such as
CLM-9001 (a prompt-injection narrative) and CLM-9002 (no incident date) live in
the same folder on purpose, and must not count towards any accuracy figure.

Run:  uv run python test_tools.py            (the 20 development claims)
      uv run python test_tools.py --all      (all 60 official claims)
"""

import os
# No offline test sends anything to Langfuse: without this, a test that
# starts the A2A server would pick up the real keys from .env and post
# spans, which is how an automated run came to report a 401.
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")

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
MANIFEST = ROOT / "data" / "manifest.json"

# Which fraud patterns a rule can catch, and which need an agent instead
NEEDS_AGENT = {
    "phantom_part": "Damage Analyst: compare billed parts with the photos (Phase-3)",
    "excluded_wear_tear": "Policy Analyst: recognise wear and tear as an exclusion",
}


def official_claims(use_all: bool) -> list:
    """The claims named in the manifest, read from disk in manifest order.

    Selecting from the manifest rather than globbing the folder is what keeps
    demonstration fixtures out of the official numbers."""
    if not MANIFEST.exists():
        sys.exit("data/manifest.json is missing. Run generate_data.py first.")
    entries = json.loads(MANIFEST.read_text())
    if not use_all:
        entries = [e for e in entries if e["split"] == "dev"]
    claims, missing = [], []
    for entry in entries:
        path = CLAIMS / f"{entry['claim_id']}.json"
        if path.exists():
            claims.append(json.loads(path.read_text()))
        else:
            missing.append(entry["claim_id"])
    if missing:
        print(f"warning: {len(missing)} claim(s) in the manifest are not on disk: "
              f"{', '.join(missing[:5])}")
    return claims


def run_checks(claim: dict) -> dict:
    """What an Assessment crew would see: the invoice lines and dates, no labels."""
    lines = [{"description": i["description"], "amount": i["amount"]} for i in claim["line_items"]]
    parts = sorted({_part_name(i["description"]) for i in claim["line_items"]} - {None})
    return {
        "period": check_policy_period(claim["policy_no"], claim.get("incident_date")),
        "prices": check_part_prices(lines),
        "prior": find_prior_claims(claim["policy_no"], claim["claim_id"],
                                   claim.get("incident_date"), parts),
    }


def main() -> None:
    use_all = "--all" in sys.argv
    claims = official_claims(use_all)
    if not claims:
        sys.exit("No claims found. Run generate_data.py first.")
    expected = 60 if use_all else 20
    print(f"{len(claims)} claim(s) from data/manifest.json "
          f"({'all 60 official' if use_all else 'development split'})")
    if use_all:
        print("note: this includes the 40 held-out test claims. They are for the "
              "final evaluation, so do not quote these figures as Phase-2 accuracy "
              "or tune prompts against them.")
    if len(claims) != expected:
        print(f"warning: expected {expected} claim(s), got {len(claims)}")
    print()

    by_fault = defaultdict(lambda: {"n": 0, "caught": 0, "how": set()})
    print(f"{'claim':<10} {'fault':<20} {'period':<8} {'prices':<8} {'prior':<8} flagged by")
    print("-" * 78)

    for claim in claims:
        fault = claim["ground_truth"]["fault_type"]
        r = run_checks(claim)
        hits = []
        # A tool that could not answer reports an error. That is not a flag:
        # an unknown date must never read as "outside the policy period".
        if "error" not in r["period"] and not r["period"]["within_period"]:
            hits.append("policy period")
        if "error" not in r["prices"] and r["prices"]["suspicious"]:
            hits.append("price check")
        if "error" not in r["prior"] and r["prior"]["possible_duplicate"]:
            hits.append("prior claims")

        row = by_fault[fault]
        row["n"] += 1
        if hits:
            row["caught"] += 1
            row["how"].update(hits)

        period = ("n/a" if "error" in r["period"]
                  else "ok" if r["period"]["within_period"] else "OUTSIDE")
        prices = ("n/a" if "error" in r["prices"]
                  else "FLAG" if r["prices"]["suspicious"] else "ok")
        prior = ("n/a" if "error" in r["prior"]
                 else "DUP" if r["prior"]["possible_duplicate"] else "ok")
        print(f"{claim['claim_id']:<10} {fault:<20} {period:<8} {prices:<8} {prior:<8} "
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
