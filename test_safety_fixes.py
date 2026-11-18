"""
The two safety fixes from the 18 Nov confirmatory set, without a token.

On gpt-oss-120b, CLM-5006 (incident 37 days before cover began) was
auto-approved for Rs 22,364: the decision guardrail rejected a correct
rejection (it also cited IMT 21), then an "uncertain", and accepted a third
answer that said "covered" on a sentence the policy does not contain. And
CLM-5014 failed outright when the guardrail ran out of retries.

  A. The policy period is enforced in code, after the crew
     within_period false + the model says "covered"  -> rejected, not approved
     the date comes from the claim as submitted, not the Intake Officer's copy
     a tool error (no policy, bad date) changes nothing
     a claim inside the period is left exactly as the model decided

  B. Guardrail exhaustion escalates to a person instead of failing
     exhausted -> human review with the reason, queued, the log finished once
     an infrastructure error (MCP timeout) still fails, as before
     exhausted on a claim outside the period -> rejected in code

  Replays of the REAL 120b outputs (test_fixtures/replay, from Langfuse),
  run through the real decision_guardrail and the Flow's own steps:
     CLM-5006: the three attempts -> rejected, Rs 0 (was: approved Rs 22,364)
     CLM-5014: the three attempts -> reaches a person (was: failed)

Run:  CLAIMSIGHT_OFFLINE=1 uv run python test_safety_fixes.py
"""

import os
os.environ.setdefault("CLAIMSIGHT_OFFLINE", "1")
os.environ.pop("CLAIMSIGHT_LOG_ARCHIVE", None)

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import claim_flow                                   # noqa: E402
import guardrails                                   # noqa: E402
import monitoring                                   # noqa: E402
from models import AssessmentDecision               # noqa: E402

REPLAY = ROOT / "test_fixtures" / "replay"


def intake_for(claim_id: str, **override) -> dict:
    """An intake built from the claim file, as the Intake crew would assemble it."""
    claim = json.loads((ROOT / "data" / "claims" / f"{claim_id}.json").read_text())
    policy = json.loads((ROOT / "data" / "policies" / f"{claim['policy_no']}.json").read_text())
    lines = [{k: l[k] for k in ("description", "amount", "material")} for l in claim["line_items"]]
    intake = {"claim_id": claim_id, "policy_no": claim["policy_no"],
              "incident_date": claim["incident_date"], "narrative": claim.get("narrative", ""),
              "damaged_parts": [], "registration": policy["registration"],
              "invoice": {"line_items": lines, "total": claim.get("invoice_total"),
                          "registration": policy["registration"]}}
    intake.update(override)
    return intake


def covered_decision(intake: dict) -> dict:
    return {"claim_id": intake["claim_id"], "coverage_status": "covered", "is_covered": True,
            "coverage_reasons": ['"Structural repairs and replacements are covered" (Section I item 5)'],
            "uncertainty_reasons": [], "fraud_score": 0.1, "fraud_flags": [],
            "line_decisions": [{**l, "admitted": True, "reason": ""}
                               for l in intake["invoice"]["line_items"]],
            "recommended_route": "auto_approve", "summary": "covered"}


class _Output:
    """What CrewAI hands a task guardrail, enough for decision_guardrail."""
    def __init__(self, raw: str, claim_id: str):
        self.raw = raw
        self.description = f"Decide claim {claim_id}."
        self.pydantic = None
        self.json_dict = json.loads(raw)


def replay_crew(claim_id: str, attempts: list, max_retries: int = 2):
    """Stand-in for run_assessment_step that behaves as CrewAI does with the
    Lead's guardrail: each attempt goes through the REAL decision_guardrail; the
    first that passes is the result; after the retries, the same exception."""
    seen = []

    def run(_inputs, _verbose):
        error = None
        for raw in attempts[:max_retries + 1]:
            ok, result = guardrails.decision_guardrail(_Output(raw, claim_id))
            seen.append(ok)
            if ok:
                return AssessmentDecision.model_validate_json(raw).model_dump()
            error = result
        raise Exception(f"Task failed guardrail validation after {max_retries} retries. "
                        f"Last error: {error}")
    return run, seen


def main() -> None:
    passed = total = 0

    def check(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += bool(condition)
        print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))

    work = Path(tempfile.mkdtemp(prefix="claimsight-safety-"))
    monitoring.LOGS = work / "logs"
    claim_flow.DECISIONS = work / "decisions"
    claim_flow.REVIEW_QUEUE = work / "review_queue"
    real_step = claim_flow.run_assessment_step

    def run_flow(claim_id: str, step, intake: dict = None):
        """intake -> assess -> route -> the routed step, as the Flow runs them."""
        claim_flow.run_assessment_step = step
        try:
            flow = claim_flow.ClaimFlow()
            flow.state.claim_id = claim_id
            flow.state.interactive = False
            flow.state.intake = intake or intake_for(claim_id)
            monitoring.ClaimMonitor(claim_id)
            error = None
            try:
                flow.assess()
                route = flow.route_claim()
                {"auto_approve": flow.approve_claim, "reject": flow.reject_claim,
                 "human_review": flow.send_to_adjuster}[route]()
            except Exception as exc:                     # noqa: BLE001
                error = exc
            return flow, error
        finally:
            claim_flow.run_assessment_step = real_step

    def log_events(claim_id):
        return [json.loads(l) for l in (monitoring.LOGS / f"{claim_id}.jsonl").read_text().splitlines()]

    print("A. The policy period, enforced in code\n")
    i5006 = intake_for("CLM-5006")
    d, override = claim_flow.enforce_policy_period(i5006, covered_decision(i5006))
    route, why = claim_flow.decide_route(d, {"payout": 0.0}, [], claim_flow.coverage_doubts(i5006, d))
    check("outside the period + model says covered -> excluded", d["coverage_status"] == "excluded")
    check("... every line denied, so nothing can be paid",
          all(l["admitted"] is False for l in d["line_decisions"]))
    check("... routed to reject, not auto-approve", route == "reject", why)
    check("... what the model said is kept for the record",
          override and override["model_coverage_status"] == "covered"
          and d["period_override"]["model_coverage_reasons"])
    check("... the reason names the dates and says it was checked in code",
          "2026-06-05" in d["coverage_reasons"][0] and "in code" in d["coverage_reasons"][0],
          d["coverage_reasons"][0])
    for status in ("uncertain", "covered"):
        dd = {**covered_decision(i5006), "coverage_status": status}
        dd, _ = claim_flow.enforce_policy_period(i5006, dd)
        r, _ = claim_flow.decide_route(dd, {"payout": 0.0}, [], claim_flow.coverage_doubts(i5006, dd))
        check(f"model says '{status}' outside the period -> reject", r == "reject")

    misread = intake_for("CLM-5006", incident_date="2026-08-01")      # inside the cover dates
    d2, _ = claim_flow.enforce_policy_period(misread, covered_decision(misread))
    check("the submitted date decides, not the Intake Officer's copy (misread into cover)",
          d2["coverage_status"] == "excluded")
    i5001 = intake_for("CLM-5001")
    d3, o3 = claim_flow.enforce_policy_period({**i5001, "incident_date": "2001-01-01"},
                                              covered_decision(i5001))
    check("... and a misread date cannot push an in-cover claim out of cover",
          d3["coverage_status"] == "covered" and o3 is None)
    before = covered_decision(i5001)
    d4, o4 = claim_flow.enforce_policy_period(i5001, before)
    check("a claim inside the period is left exactly as the model decided",
          d4 == before and o4 is None)
    ghost = {**i5001, "claim_id": "CLM-0000", "policy_no": "POL-NOPE"}
    d5, o5 = claim_flow.enforce_policy_period(ghost, covered_decision(ghost))
    check("a tool error (no policy on file) changes nothing", o5 is None and d5["coverage_status"] == "covered")
    excluded = {**covered_decision(i5006), "coverage_status": "excluded", "is_covered": False}
    d6, o6 = claim_flow.enforce_policy_period(i5006, excluded)
    check("a model that already rejected is not reported as overridden",
          o6 is None and d6["coverage_status"] == "excluded")

    print("\nB. Guardrail exhaustion escalates to a person\n")

    def always_exhausted(_i, _v):
        raise Exception("Task failed guardrail validation after 2 retries. Last error: "
                        "Fix these problems in your decision: something the guardrail refused")
    flow, err = run_flow("CLM-5001", always_exhausted)
    events = log_events("CLM-5001")
    finished = [e for e in events if e["event"] == "run_finished"]
    check("no error reaches the caller", err is None, repr(err))
    check("routed to human review", flow.state.route == "human_review", flow.state.route)
    check("status awaiting_review, queued for an adjuster",
          flow.state.status == "awaiting_review"
          and (claim_flow.REVIEW_QUEUE / "CLM-5001.json").exists())
    check("the reason a person must look is on the record",
          any("could not produce a decision" in f for f in flow.state.guardrail_flags),
          str(flow.state.guardrail_flags))
    check("the log is finished once, as awaiting_review (not as failed)",
          len(finished) == 1 and finished[0]["status"] == "awaiting_review",
          str([f.get("status") for f in finished]))
    check("the fraud risk is marked as not assessed",
          any("NOT assessed" in f for f in flow.state.decision["fraud_flags"]))
    check("the adjuster still sees a provisional payout",
          flow.state.settlement.get("payout", 0) > 0, str(flow.state.settlement.get("payout")))

    def mcp_timeout(_i, _v):
        raise RuntimeError("MCP connection timed out after 30 seconds.")
    flow, err = run_flow("CLM-5001", mcp_timeout)
    finished = [e for e in log_events("CLM-5001") if e["event"] == "run_finished"]
    check("an infrastructure error still fails, as before",
          isinstance(err, RuntimeError) and finished and finished[-1]["status"] == "failed")

    flow, err = run_flow("CLM-5006", always_exhausted)
    check("exhausted on a claim outside the period -> rejected in code",
          err is None and flow.state.route == "reject" and flow.state.final_payout == 0.0,
          f"{flow.state.route} {err!r}")

    print("\nReplay: CLM-5006, the real 120b outputs\n")
    attempts = [a["raw"] for a in json.loads((REPLAY / "CLM-5006_120b_lead_attempts.json").read_text())["attempts"]]
    step, seen = replay_crew("CLM-5006", attempts)
    flow, err = run_flow("CLM-5006", step)
    check("the guardrail behaves as it did on 18 Nov (refused, refused, accepted)",
          seen == [False, False, True], str(seen))
    check("the accepted answer said 'covered'",
          flow.state.decision.get("period_override", {}).get("model_coverage_status") == "covered")
    check("the claim is now REJECTED, not auto-approved",
          err is None and flow.state.route == "reject" and flow.state.status == "rejected",
          f"{flow.state.route} / {flow.state.status} {err!r}")
    check("payout Rs 0 (was Rs 22,364.00)", flow.state.final_payout == 0.0)
    check("the override is in the claim's log",
          any(e.get("kind") == "period_override" for e in log_events("CLM-5006")))
    for n, raw in enumerate(attempts, 1):
        dd, _ = claim_flow.enforce_policy_period(i5006, AssessmentDecision.model_validate_json(raw).model_dump())
        r, _ = claim_flow.decide_route(dd, {"payout": 0.0}, [], claim_flow.coverage_doubts(i5006, dd))
        check(f"whichever attempt had passed (attempt {n}) -> reject", r == "reject")

    print("\nReplay: CLM-5014, the real 120b outputs\n")
    attempts = [a["raw"] for a in json.loads((REPLAY / "CLM-5014_120b_lead_attempts.json").read_text())["attempts"]]
    step, seen = replay_crew("CLM-5014", attempts)
    flow, err = run_flow("CLM-5014", step)
    check("the guardrail behaves as it did on 18 Nov (refused all three)",
          seen == [False, False, False], str(seen))
    check("the claim REACHES A PERSON instead of failing",
          err is None and flow.state.route == "human_review" and flow.state.status == "awaiting_review",
          f"{flow.state.route} / {flow.state.status} {err!r}")
    check("the adjuster is told why: the guardrail's last objection (IMT 21)",
          any("IMT 21" in f for f in flow.state.guardrail_flags), str(flow.state.guardrail_flags)[:300])
    check("queued in the review queue", (claim_flow.REVIEW_QUEUE / "CLM-5014.json").exists())

    print(f"\n{passed}/{total} safety checks as expected")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
