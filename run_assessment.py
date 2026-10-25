"""
Run the Assessment crew on claims, compute each payout with the settlement
calculator, and score the result against the ground truth.

By default it runs a six-claim demo set, one claim per pattern:
  CLM-5000 clean            CLM-5004 inflated amount   CLM-5005 phantom part
  CLM-5006 outside period   CLM-5007 wear and tear     CLM-5008 duplicate

Run:  uv run python run_assessment.py                 (demo set)
      uv run python run_assessment.py CLM-5004        (one claim)
      uv run python run_assessment.py --dev           (all 20 dev claims)
      add --quiet to hide the agent logs
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from models import AssessmentDecision                     # noqa: E402
from guardrails import safe_narrative                     # noqa: E402
from tools.assessment_tools import _part_name, settle     # noqa: E402

CLAIMS = ROOT / "data" / "claims"
POLICIES = ROOT / "data" / "policies"
INTAKE = ROOT / "data" / "intake"
OUT = ROOT / "data" / "assessment"

DEMO_SET = ["CLM-5000", "CLM-5004", "CLM-5005", "CLM-5006", "CLM-5007", "CLM-5008"]
FRAUD_THRESHOLD = 0.5
PAUSE_SECONDS = 20      # between claims, to stay inside the free-tier token limit

# Only what assessment needs. Name, phone and address never reach the LLM.
POLICY_FIELDS = ["policy_no", "make", "model", "vehicle_age_years", "idv", "deductible",
                 "policy_start", "policy_end", "zero_depreciation"]


# ------------------------------------------------------------ inputs
def build_case(claim_id: str):
    """The crew's inputs. Uses the Intake crew's saved output when it exists,
    otherwise the invoice data directly. Ground-truth labels are never passed."""
    claim = json.loads((CLAIMS / f"{claim_id}.json").read_text())
    policy = json.loads((POLICIES / f"{claim['policy_no']}.json").read_text())

    intake_path = INTAKE / f"{claim_id}.json"
    if intake_path.exists():
        intake = json.loads(intake_path.read_text())
        lines = [{"description": i["description"], "amount": i["amount"], "material": i.get("material")}
                 for i in intake["invoice"]["line_items"]]
        parts = intake["damaged_parts"]
        incident = intake.get("incident_date") or claim["incident_date"]
        source = "Intake crew output"
    else:
        lines = [{"description": i["description"], "amount": i["amount"], "material": i["material"]}
                 for i in claim["line_items"]]
        parts = sorted({_part_name(i["description"]) for i in claim["line_items"]} - {None})
        incident = claim["incident_date"]
        source = "invoice data (Intake crew not run for this claim)"

    inputs = {
        "claim_id": claim_id,
        "policy_no": claim["policy_no"],
        "incident_date": incident,
        "narrative": safe_narrative(claim["narrative"])[0],
        "damaged_parts": json.dumps(parts),
        "line_items": json.dumps(lines, indent=1),
        "policy": json.dumps({k: policy[k] for k in POLICY_FIELDS}, indent=1),
    }
    return inputs, claim, source


# ------------------------------------------------------------ outputs
def normalise_route(route: str) -> str:
    route = route.lower().strip().replace("-", "_").replace(" ", "_")
    return route if route in {"auto_approve", "human_review", "reject"} else "human_review"


def finalise(decision: AssessmentDecision, claim: dict) -> dict:
    """Payout from the calculator, never from the LLM."""
    if not decision.is_covered:
        return {"payout": 0.0, "explanation": "Not covered: no settlement."}
    lines = [d.model_dump() for d in decision.line_decisions]
    return settle(claim["policy_no"], lines)


def match_line(truth_line: dict, decisions: list):
    """Pair a ground-truth line with the agent's line: same amount, then same wording."""
    same_amount = [d for d in decisions if abs(d.amount - truth_line["amount"]) < 1]
    if len(same_amount) == 1:
        return same_amount[0]
    wanted = truth_line["description"].lower().strip()
    return next((d for d in same_amount or decisions if d.description.lower().strip() == wanted), None)


def score(decision: AssessmentDecision, payout: float, claim: dict) -> dict:
    truth = claim["ground_truth"]
    line_hits = 0
    for line in claim["line_items"]:
        got = match_line(line, decision.line_decisions)
        expected_admitted = line["admitted"] and truth["is_covered"]
        if got is not None and got.admitted == expected_admitted:
            line_hits += 1
    return {
        "coverage_ok": decision.is_covered == truth["is_covered"],
        "flagged": decision.fraud_score >= FRAUD_THRESHOLD,
        "is_fraud": truth["is_fraudulent"],
        "lines_ok": line_hits,
        "lines_total": len(claim["line_items"]),
        "payout": payout,
        "expected_payout": truth["expected_payout"],
        "payout_ok": abs(payout - truth["expected_payout"]) <= 1.0,
    }


def print_claim(claim_id, decision, settlement, result, source):
    fault = result["fault"]
    print("\n" + "=" * 70)
    print(f"ASSESSMENT  {claim_id}   (true pattern: {fault}; input: {source})")
    print("=" * 70)
    print(f"covered      {decision.is_covered}")
    for reason in decision.coverage_reasons:
        print(f"             - {reason}")
    print(f"fraud score  {decision.fraud_score:.2f}")
    for flag in decision.fraud_flags:
        print(f"             - {flag}")
    print("lines")
    for line in decision.line_decisions:
        mark = "PAY " if line.admitted else "DENY"
        reason = f"  ({line.reason})" if line.reason else ""
        print(f"   {mark} {line.description}: Rs {line.amount:,.0f}{reason}")
    print(f"route        {normalise_route(decision.recommended_route)}")
    print(f"summary      {decision.summary}")
    print("\nsettlement (calculator)")
    print("   " + settlement.get("explanation", "").replace("\n", "\n   "))
    print("\nvs ground truth")
    print(f"   coverage   {'correct' if result['coverage_ok'] else 'WRONG'}")
    print(f"   fraud flag {'correct' if result['flagged'] == result['is_fraud'] else 'WRONG'}"
          f"  (flagged={result['flagged']}, actually fraud={result['is_fraud']})")
    print(f"   lines      {result['lines_ok']}/{result['lines_total']} decided correctly")
    print(f"   payout     Rs {result['payout']:,.2f} vs expected Rs {result['expected_payout']:,.2f}"
          f"  [{'match' if result['payout_ok'] else 'MISMATCH'}]")


def print_summary(results: list) -> None:
    done = [r for r in results if "error" not in r]
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"{'claim':<10} {'pattern':<20} {'cover':<6} {'fraud':<6} {'lines':<6} {'payout':<9} route")
    for r in results:
        if "error" in r:
            print(f"{r['claim_id']:<10} {r['fault']:<20} FAILED: {r['error'][:40]}")
            continue
        print(f"{r['claim_id']:<10} {r['fault']:<20} "
              f"{'ok' if r['coverage_ok'] else 'WRONG':<6} "
              f"{'ok' if r['flagged'] == r['is_fraud'] else 'WRONG':<6} "
              f"{r['lines_ok']}/{r['lines_total']:<4} "
              f"{'ok' if r['payout_ok'] else 'WRONG':<9} {r['route']}")
    if not done:
        return

    n = len(done)
    tp = sum(r["flagged"] and r["is_fraud"] for r in done)
    fp = sum(r["flagged"] and not r["is_fraud"] for r in done)
    fn = sum(not r["flagged"] and r["is_fraud"] for r in done)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    print("-" * 70)
    print(f"coverage accuracy   {sum(r['coverage_ok'] for r in done)}/{n}")
    print(f"fraud precision     {precision:.2f}   recall {recall:.2f}   (threshold {FRAUD_THRESHOLD})")
    print(f"line decisions      {sum(r['lines_ok'] for r in done)}/{sum(r['lines_total'] for r in done)}")
    print(f"payout exact        {sum(r['payout_ok'] for r in done)}/{n}")
    tokens = sum(r.get("tokens", 0) for r in done)
    if tokens:
        print(f"tokens used         {tokens:,}  (about {tokens // n:,} per claim)")
    if any(r["fault"] == "phantom_part" for r in done):
        print("note: phantom parts need the photo check (Damage Analyst, Phase-3); "
              "a miss there is expected for now.")


# ------------------------------------------------------------ main
def choose_claims(args: list) -> list:
    ids = [a for a in args if a.startswith("CLM-")]
    if ids:
        return ids
    if "--dev" in args:
        return [json.loads(f.read_text())["claim_id"] for f in sorted(CLAIMS.glob("CLM-*.json"))
                if json.loads(f.read_text())["split"] == "dev"]
    return DEMO_SET


def main() -> None:
    from config import describe                        # imported here: needs crewai
    from crews.assessment_crew import build_assessment_crew

    args = sys.argv[1:]
    verbose = "--quiet" not in args
    claim_ids = choose_claims(args)
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Assessing {len(claim_ids)} claim(s) using {describe()}")

    results = []
    for n, claim_id in enumerate(claim_ids):
        if not (CLAIMS / f"{claim_id}.json").exists():
            print(f"skip {claim_id}: no such claim")
            continue
        inputs, claim, source = build_case(claim_id)
        fault = claim["ground_truth"]["fault_type"]
        try:
            crew = build_assessment_crew(verbose=verbose)
            output = crew.kickoff(inputs=inputs)
            decision = output.pydantic
            if decision is None:
                raise ValueError("crew returned no valid AssessmentDecision")

            settlement = finalise(decision, claim)
            result = score(decision, settlement["payout"], claim)
            usage = getattr(output, "token_usage", None)
            result.update(claim_id=claim_id, fault=fault,
                          route=normalise_route(decision.recommended_route),
                          tokens=getattr(usage, "total_tokens", 0) or 0)
            print_claim(claim_id, decision, settlement, result, source)

            (OUT / f"{claim_id}.json").write_text(json.dumps(
                {"decision": decision.model_dump(), "settlement": settlement, "score": result},
                indent=2, default=str))
        except Exception as exc:                       # noqa: BLE001
            result = {"claim_id": claim_id, "fault": fault, "error": str(exc)}
            print(f"\n{claim_id} FAILED: {exc}")
        results.append(result)

        if n < len(claim_ids) - 1:
            time.sleep(PAUSE_SECONDS)

    print_summary(results)


if __name__ == "__main__":
    main()
