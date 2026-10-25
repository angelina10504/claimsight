"""
ClaimSight Flow: the full claim pipeline as one CrewAI Flow.

    @start   intake            Intake crew -> ClaimIntake
    @router  check_complete    "complete" / "incomplete"
       incomplete -> request_evidence   (ask the claimant for what is missing)
       complete   -> assess             Assessment crew -> AssessmentDecision,
                                         then the settlement calculator
    @router  route_claim       "auto_approve" / "human_review" / "reject"
       auto_approve -> approve_claim
       human_review -> send_to_adjuster  (HUMAN GATE: the flow pauses for a person)
       reject       -> reject_claim

The routers are plain Python reading typed fields. The LLM recommends a route,
but the code decides it, so the same evidence always takes the same path.

@persist saves the flow's state after every step (checkpointing), so each
claim's full trail is kept even if a run stops part-way.
"""

import json
import sys
import time
from pathlib import Path
from typing import Dict, List

from crewai.flow.flow import Flow, listen, router, start
from crewai.flow.persistence import persist
from pydantic import BaseModel, Field

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

import guardrails                            # noqa: E402
import monitoring                            # noqa: E402
from tools.assessment_tools import settle   # noqa: E402

CLAIMS = ROOT / "data" / "claims"
POLICIES = ROOT / "data" / "policies"
INTAKE = ROOT / "data" / "intake"
DECISIONS = ROOT / "data" / "decisions"
REVIEW_QUEUE = ROOT / "data" / "review_queue"

FRAUD_THRESHOLD = 0.5
HIGH_VALUE = 50_000          # payouts above this always go to a person
POLICY_FIELDS = ["policy_no", "make", "model", "vehicle_age_years", "idv", "deductible",
                 "policy_start", "policy_end", "zero_depreciation"]


class ClaimState(BaseModel):
    claim_id: str = ""
    use_saved_intake: bool = True     # reuse data/intake/<id>.json if it exists
    interactive: bool = True          # ask the adjuster at the human gate
    verbose: bool = False
    intake: Dict = Field(default_factory=dict)
    decision: Dict = Field(default_factory=dict)
    settlement: Dict = Field(default_factory=dict)
    route: str = ""
    status: str = ""
    final_payout: float = 0.0
    guardrail_flags: List[str] = Field(default_factory=list)
    trail: List[str] = Field(default_factory=list)


# ----------------------------------------------------- crew steps
# Kept as module-level functions so they can be swapped out in tests.
def run_intake_step(claim_id: str, verbose: bool) -> dict:
    from crews.intake_crew import run_intake
    intake = run_intake(claim_id, verbose=verbose, monitor=monitoring.current())
    if intake is None:
        raise RuntimeError("Intake crew returned no valid ClaimIntake")
    return intake.model_dump()


def run_assessment_step(inputs: dict, verbose: bool) -> dict:
    from crews.assessment_crew import build_assessment_crew
    monitor = monitoring.current()
    started = time.time()
    output = build_assessment_crew(verbose=verbose, monitor=monitor).kickoff(inputs=inputs)
    usage = getattr(output, "token_usage", None)
    if monitor:
        monitor.record("crew_done", crew="assessment", seconds=round(time.time() - started, 2),
                       total_tokens=getattr(usage, "total_tokens", 0) or 0,
                       prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                       completion_tokens=getattr(usage, "completion_tokens", 0) or 0)
    if output.pydantic is None:
        raise RuntimeError("Assessment crew returned no valid AssessmentDecision")
    return output.pydantic.model_dump()


def completeness_gaps(intake: dict) -> list:
    """The four completeness rules, checked in code. The Intake Officer also
    gives a verdict, but a fact like 'is there a total' should not depend on
    an LLM: in testing it once marked a complete file incomplete with no reason."""
    gaps = []
    policy_path = POLICIES / f"{intake.get('policy_no', '')}.json"
    if not policy_path.exists():
        return ["policy schedule (policy number not found)"]
    policy = json.loads(policy_path.read_text())
    invoice = intake.get("invoice") or {}

    if not invoice.get("line_items"):
        gaps.append("invoice line items")
    if not invoice.get("total"):
        gaps.append("invoice total")
    if not intake.get("incident_date"):
        gaps.append("incident date")

    def plate(text):
        return (text or "").replace(" ", "").upper()

    billed = plate(invoice.get("registration") or intake.get("registration"))
    if not billed:
        gaps.append("vehicle registration on the invoice")
    elif billed != plate(policy["registration"]):
        gaps.append("invoice registration does not match the policy")
    return gaps


def assessment_inputs(intake: dict) -> dict:
    """What the Assessment crew sees. Name, phone and address are left out."""
    policy = json.loads((POLICIES / f"{intake['policy_no']}.json").read_text())
    lines = [{"description": i["description"], "amount": i["amount"], "material": i.get("material")}
             for i in intake["invoice"]["line_items"]]
    return {
        "claim_id": intake["claim_id"],
        "policy_no": intake["policy_no"],
        "incident_date": intake["incident_date"],
        "narrative": intake.get("narrative") or "",
        "damaged_parts": json.dumps(intake.get("damaged_parts", [])),
        "line_items": json.dumps(lines, indent=1),
        "policy": json.dumps({k: policy[k] for k in POLICY_FIELDS}, indent=1),
    }


# ----------------------------------------------------- the flow
@persist()
class ClaimFlow(Flow[ClaimState]):

    def log(self, message: str, **fields) -> None:
        self.state.trail.append(message)
        print(f"  [{self.state.claim_id}] {message}")
        monitor = monitoring.current()
        if monitor:
            monitor.record("flow", message=message, **fields)

    def guarded(self, step: str, fn, *args):
        """Run a crew step; log any error with where it happened, then re-raise."""
        try:
            return fn(*args)
        except Exception as exc:
            monitor = monitoring.current()
            if monitor:
                monitor.error(step, exc)
                monitor.finish(status="failed", failed_at=step)
            raise

    def screen_claimant_text(self) -> None:
        """Input guardrail: check what the claimant wrote before any agent reads it."""
        claim = json.loads((CLAIMS / f"{self.state.claim_id}.json").read_text())
        result = guardrails.screen_text(claim.get("narrative", ""))
        monitoring.current().record("guardrail", kind="input_screening",
                                    passed=result["verdict"] == "clean", **result)
        if result["verdict"] == "injection":
            self.state.guardrail_flags.append(
                f"possible prompt injection in claimant text: {result['injection_matches'][:2]}")
            self.log("guardrail: prompt injection detected; narrative withheld from the agents")
        elif result["verdict"] == "abusive":
            self.state.guardrail_flags.append("abusive language in claimant text")
            self.log("guardrail: abusive language masked before the agents see it")

    @start()
    def intake(self):
        monitoring.ClaimMonitor(self.state.claim_id)     # fresh log: data/logs/<claim>.jsonl
        self.screen_claimant_text()
        saved = INTAKE / f"{self.state.claim_id}.json"
        if self.state.use_saved_intake and saved.exists():
            self.state.intake = json.loads(saved.read_text())
            self.log("intake: reused saved Intake crew output")
        else:
            self.log("intake: running Intake crew")
            started = time.time()
            self.state.intake = self.guarded("intake", run_intake_step,
                                             self.state.claim_id, self.state.verbose)
            monitoring.current().record("crew_done", crew="intake",
                                        seconds=round(time.time() - started, 2))
            INTAKE.mkdir(parents=True, exist_ok=True)
            saved.write_text(json.dumps(self.state.intake, indent=2))

        # The agents only ever see the screened narrative, even from a saved intake
        self.state.intake["narrative"], _ = guardrails.safe_narrative(
            self.state.intake.get("narrative") or "")

        gaps = completeness_gaps(self.state.intake)
        agent_said = self.state.intake.get("is_complete")
        if agent_said != (not gaps):
            self.log(f"intake: Intake Officer said complete={agent_said}; "
                     f"the completeness check overrides it")
        self.state.intake["is_complete"] = not gaps
        self.state.intake["missing_fields"] = gaps
        lines = len(self.state.intake["invoice"]["line_items"])
        self.log(f"intake: {lines} invoice lines, complete={not gaps}")

    @router(intake, emit=["complete", "incomplete"])
    def check_complete(self):
        if self.state.intake.get("is_complete"):
            self.log("router 1: file complete -> assessment")
            return "complete"
        self.log(f"router 1: file incomplete -> evidence request {self.state.intake.get('missing_fields')}")
        return "incomplete"

    @listen("incomplete")
    def request_evidence(self):
        missing = ", ".join(self.state.intake.get("missing_fields") or ["details not stated"])
        self.state.route = "needs_evidence"
        self.state.status = "awaiting_evidence"
        self.log(f"asked the claimant for: {missing}")
        self.finish()

    @listen("complete")
    def assess(self):
        self.log("assessment: Policy Analyst and Fraud Analyst running in parallel")
        self.state.decision = self.guarded("assessment", run_assessment_step,
                                           assessment_inputs(self.state.intake), self.state.verbose)
        d = self.state.decision
        if d["is_covered"]:
            self.state.settlement = settle(self.state.intake["policy_no"], d["line_decisions"])
        else:
            self.state.settlement = {"payout": 0.0, "explanation": "Not covered: no settlement."}
        denied = sum(not line["admitted"] for line in d["line_decisions"])
        self.log(f"assessment: covered={d['is_covered']}, fraud_score={d['fraud_score']:.2f}, "
                 f"{denied} line(s) denied, payout Rs {self.state.settlement['payout']:,.2f} (calculator)")

    @router(assess, emit=["auto_approve", "human_review", "reject"])
    def route_claim(self):
        d, payout = self.state.decision, self.state.settlement["payout"]
        if self.state.guardrail_flags:
            route, why = "human_review", f"guardrail flag: {self.state.guardrail_flags[0]}"
        elif not d["is_covered"]:
            route, why = "reject", "not covered"
        elif d["fraud_score"] >= FRAUD_THRESHOLD:
            route, why = "human_review", f"fraud score {d['fraud_score']:.2f} >= {FRAUD_THRESHOLD}"
        elif payout > HIGH_VALUE:
            route, why = "human_review", f"payout above Rs {HIGH_VALUE:,}"
        else:
            route, why = "auto_approve", "covered, low fraud risk, normal value"
        suggested = d.get("recommended_route", "").lower().replace("-", "_").replace(" ", "_")
        if suggested and suggested != route:
            why += f" (the Lead suggested {suggested}; the rule decides)"
        self.state.route = route
        self.log(f"router 2: {route} because {why}", router="route_claim", route=route,
                 fraud_score=d["fraud_score"], payout=payout)
        return route

    @listen("auto_approve")
    def approve_claim(self):
        self.state.status = "approved"
        self.state.final_payout = self.state.settlement["payout"]
        self.log(f"auto-approved: pay Rs {self.state.final_payout:,.2f}")
        self.finish()

    @listen("reject")
    def reject_claim(self):
        self.state.status = "rejected"
        self.state.final_payout = 0.0
        reasons = "; ".join(self.state.decision.get("coverage_reasons", [])[:2])
        self.log(f"rejected: {reasons}")
        self.finish()

    @listen("human_review")
    def send_to_adjuster(self):
        d, payout = self.state.decision, self.state.settlement["payout"]
        print("\n" + "-" * 70)
        print(f"HUMAN REVIEW  {self.state.claim_id}")
        print("-" * 70)
        print(f"fraud score   {d['fraud_score']:.2f}")
        for flag in d.get("fraud_flags", []):
            print(f"              - {flag}")
        for line in d["line_decisions"]:
            mark = "PAY " if line["admitted"] else "DENY"
            note = f"  ({line['reason']})" if line.get("reason") else ""
            print(f"  {mark} {line['description']}: Rs {line['amount']:,.0f}{note}")
        print(f"proposed payout  Rs {payout:,.2f}")
        print(f"summary       {d.get('summary', '')}")

        if not self.state.interactive:
            REVIEW_QUEUE.mkdir(parents=True, exist_ok=True)
            (REVIEW_QUEUE / f"{self.state.claim_id}.json").write_text(
                json.dumps(self.state.model_dump(), indent=2, default=str))
            self.state.status = "awaiting_review"
            self.log("paused: queued for an adjuster (data/review_queue)")
            self.finish()
            return

        answer = input("\nAdjuster: [a]pprove, [r]eject, or type an amount to approve: ").strip().lower()
        if answer.startswith("r"):
            self.state.status, self.state.final_payout = "rejected_by_adjuster", 0.0
        else:
            try:
                amount = float(answer.replace(",", "")) if answer and not answer.startswith("a") else payout
            except ValueError:
                amount = payout
            self.state.status, self.state.final_payout = "approved_by_adjuster", amount
        self.log(f"adjuster decision: {self.state.status}, pay Rs {self.state.final_payout:,.2f}")
        self.finish()

    def finish(self) -> None:
        monitor = monitoring.current()
        if monitor:
            monitor.finish(status=self.state.status, route=self.state.route,
                           final_payout=self.state.final_payout)
        DECISIONS.mkdir(parents=True, exist_ok=True)
        (DECISIONS / f"{self.state.claim_id}.json").write_text(
            json.dumps(self.state.model_dump(), indent=2, default=str))
