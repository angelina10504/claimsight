"""
Test how a claim is routed, including the new "uncertain coverage" state.

Costs no tokens: the routing rules are plain Python, so this feeds them
hand-written decisions and checks where each claim would go. The point being
tested is that ClaimSight never rejects a claim on an unsupported exclusion.

    confirmed exclusion (clause cited)   -> reject
    exclusion with no clause named       -> a person decides
    outside the policy period            -> reject (arithmetic, not judgement)
    coverage could not be established    -> a person decides
    covered, suspicious price            -> a person decides
    covered, high value                  -> a person decides
    covered, low risk, normal value      -> auto-approve

Run:  uv run python test_routing.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from claim_flow import (                                            # noqa: E402
    cites_policy,
    coverage_doubts,
    decide_route,
    payout_doubts as cf_payout_doubts,
)
from models import AssessmentDecision                               # noqa: E402

CLAIMS = ROOT / "data" / "claims"
LINES = [{"description": "Front bumper - replacement", "amount": 9000.0,
          "material": "plastic", "admitted": True, "reason": ""}]


def decision(status, reasons, score=0.1, admitted=True, uncertainty=()):
    return {"claim_id": "CLM-0000", "coverage_status": status,
            "coverage_reasons": list(reasons), "uncertainty_reasons": list(uncertainty),
            "fraud_score": score, "fraud_flags": [],
            "line_decisions": [{**LINES[0], "admitted": admitted}],
            "recommended_route": "auto_approve", "summary": "test"}


def route_for(intake, d, settlement=None, flags=()):
    settlement = settlement or {"payout": 9000.0}
    return decide_route(d, settlement, list(flags), coverage_doubts(intake, d))


def find_claim(fault):
    """A generated claim with this hidden fault, so the test uses real data."""
    for path in sorted(CLAIMS.glob("CLM-*.json")):
        claim = json.loads(path.read_text())
        if claim.get("ground_truth", {}).get("fault_type") == fault:
            return claim
    return None


def main() -> None:
    normal = {"policy_no": "POL-3000", "incident_date": "2026-09-18"}
    cases = [
        ("covered, low risk, normal value",
         normal, decision("covered", ["Section I item 6: accidental external means"]),
         None, (), "auto_approve"),
        ("exclusion with the clause cited",
         normal, decision("excluded", ["Section II exclusion 4: wear and tear"], admitted=False),
         None, (), "reject"),
        ("exclusion with no clause named",
         normal, decision("excluded", ["this looks like wear and tear to me"], admitted=False),
         None, (), "human_review"),
        ("exclusion quoting the real policy wording",
         normal, decision("excluded", ['the policy excludes "consequential loss, depreciation"'],
                          admitted=False),
         None, (), "reject"),
        ("coverage could not be established",
         normal, decision("uncertain", ["no clause found either way"],
                          uncertainty=["the wording does not cover this cause"]),
         None, (), "human_review"),
        ("covered but the price looks wrong",
         normal, decision("covered", ["Section I item 6"], score=0.7),
         None, (), "human_review"),
        ("covered but a high payout",
         normal, decision("covered", ["Section I item 6"]),
         {"payout": 60_000.0}, (), "human_review"),
        ("a guardrail fired on the claimant's text",
         normal, decision("covered", ["Section I item 6"]),
         None, ("possible prompt injection",), "human_review"),
        ("an old decision saved before coverage_status existed",
         normal, {"claim_id": "CLM-0000", "is_covered": True, "coverage_reasons": ["Section I"],
                  "fraud_score": 0.1, "line_decisions": LINES, "summary": ""},
         None, (), "auto_approve"),
    ]

    outside = find_claim("date_outside_policy")
    if outside:
        cases.append((f"outside the policy period ({outside['claim_id']}, no clause needed)",
                      {"policy_no": outside["policy_no"], "incident_date": outside["incident_date"]},
                      decision("excluded", ["the incident is after cover ended"], admitted=False),
                      None, (), "reject"))

    passed = 0
    print("Where does each claim go?\n")
    for name, intake, d, settlement, flags, expected in cases:
        route, why = route_for(intake, d, settlement, flags)
        ok = route == expected
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        -> {route}  ({why})")
        if not ok:
            print(f"        expected {expected}")

    print("\nA payout the calculator cannot compute safely goes to a person:")
    tax_cases = [
        ("the invoice lists amounts before tax, as expected",
         {"invoice": {"line_items": [{"amount": 100}, {"amount": 100}], "total": 236}},
         "auto_approve"),
        ("the invoice amounts already include tax",
         {"invoice": {"line_items": [{"amount": 100}, {"amount": 100}], "total": 200}},
         "human_review"),
        ("the invoice total matches neither",
         {"invoice": {"line_items": [{"amount": 100}], "total": 900}},
         "human_review"),
    ]
    for name, intake, expected in tax_cases:
        problems = cf_payout_doubts(intake)
        route, why = decide_route(decision("covered", ["Section I item 6"]),
                                  {"payout": 200.0}, [], [], problems)
        ok = route == expected
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}\n        -> {route}  ({why})")

    print("\nThe model never pays out on an unsettled coverage question:")
    model_checks = [
        ("uncertain, even if the agent sets is_covered=true",
         {"coverage_status": "uncertain", "is_covered": True,
          "uncertainty_reasons": ["unclear"]}, False),
        ("covered", {"coverage_status": "covered"}, True),
        ("excluded", {"coverage_status": "excluded"}, False),
    ]
    for name, fields, expected in model_checks:
        built = AssessmentDecision(claim_id="CLM-0000", fraud_score=0.1, line_decisions=[],
                                   coverage_reasons=["Section I"], recommended_route="human_review",
                                   summary="t", **fields)
        ok = built.is_covered == expected
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: is_covered={built.is_covered}")

    print("\nIs a coverage reason grounded in the policy?")
    citation_checks = [("Section II exclusion 4", True),
                       ("item 6 of Section I", True),
                       ("wear and tear is not covered", False),
                       ('the wording says "by accidental external means"', True)]
    for reason, expected in citation_checks:
        ok = cites_policy(reason) == expected
        passed += ok
        print(f"  {'PASS' if ok else 'FAIL'}  {reason!r} -> grounded={cites_policy(reason)}")

    total = len(cases) + len(tax_cases) + len(model_checks) + len(citation_checks)
    print(f"\n{passed}/{total} routing checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
