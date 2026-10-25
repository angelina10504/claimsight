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
import re
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

import evidence                              # noqa: E402
import guardrails                            # noqa: E402
import observability                         # noqa: E402
import monitoring                            # noqa: E402
from tools.assessment_tools import check_policy_period, settle   # noqa: E402
from tools.settlement import check_invoice_tax                   # noqa: E402

CLAIMS = ROOT / "data" / "claims"
POLICIES = ROOT / "data" / "policies"
INTAKE = ROOT / "data" / "intake"
DECISIONS = ROOT / "data" / "decisions"
REVIEW_QUEUE = ROOT / "data" / "review_queue"
WORDING = ROOT / "knowledge" / "policy_wording.txt"

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
    coverage_doubts: List[str] = Field(default_factory=list)
    evidence_applied: List[str] = Field(default_factory=list)
    payout_doubts: List[str] = Field(default_factory=list)
    resumed: bool = False             # this run continued a claim that was waiting
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


# A clause reference such as "Section II exclusion 4", "Clause 3", "item 6".
CLAUSE_RE = re.compile(r"\b(section|clause|exclusion|exception|item|endorsement)\s*[0-9IVXivx]")
_wording_cache: List[str] = []


def _wording() -> str:
    """The policy wording as one lowercase, single-spaced string, read once."""
    if not _wording_cache:
        text = WORDING.read_text(errors="ignore") if WORDING.exists() else ""
        _wording_cache.append(" ".join(text.lower().split()))
    return _wording_cache[0]


def cites_policy(reason: str) -> bool:
    """Is this coverage reason actually grounded in the policy?

    Either it names a clause, or it quotes a long enough piece of text that
    really appears in the policy wording. A reason that does neither is the
    agent's own words, which is not enough to reject a claim on."""
    reason = reason or ""
    if CLAUSE_RE.search(reason.lower()):
        return True
    for quoted in re.findall(r"[\"\u201c\u2018']([^\"\u201d\u2019']{20,})[\"\u201d\u2019']", reason):
        if " ".join(quoted.lower().split()) in _wording():
            return True
    return False


def coverage_doubts(intake: dict, decision: dict) -> list:
    """Reasons NOT to act on the coverage verdict without a person.

    Checked in code, like completeness_gaps, because an LLM contributes to the
    coverage decision and a wrong rejection costs the claimant their payout.
    An exclusion the policy period proves is objective and needs no clause."""
    status = (decision.get("coverage_status") or "").lower()
    if not status:                      # a decision saved before this field existed
        status = "covered" if decision.get("is_covered") else "excluded"
    reasons = [r for r in (decision.get("coverage_reasons") or []) if r]

    if status == "uncertain":
        said = (decision.get("uncertainty_reasons") or ["no reason given"])[0]
        return [f"the Policy Analyst could not establish coverage: {said}"]
    if status != "excluded":
        return []

    period = check_policy_period(intake.get("policy_no") or "",
                                 intake.get("incident_date") or "1970-01-01")
    if period.get("within_period") is False:
        return []                       # outside the cover dates: arithmetic, not judgement
    if not any(cites_policy(r) for r in reasons):
        return [f"the exclusion names no policy clause: "
                f"{reasons[0] if reasons else 'no reason given'}"]
    return []


def payout_doubts(intake: dict) -> list:
    """Reasons the payout cannot be trusted even if the coverage is clear.

    The calculator depreciates each line and then adds GST, which is only
    correct if the invoice lists its amounts before tax. check_invoice_tax
    settles that from the invoice's own arithmetic instead of assuming it, so a
    tax-inclusive bill goes to a person rather than being paid twice the tax."""
    invoice = (intake or {}).get("invoice") or {}
    verdict = check_invoice_tax(invoice.get("line_items"), invoice.get("total"))
    if verdict["treatment"] == "exclusive":
        return []
    return [f"the invoice's tax treatment is {verdict['treatment']}: {verdict['note']}"]


def decide_route(decision: dict, settlement: dict, guardrail_flags: list,
                 doubts: list, payout_problems: list = ()) -> tuple:
    """The routing rules, in order. Pure function, so it can be tested without
    running a crew. Returns (route, why)."""
    payout = settlement.get("payout", 0.0)
    status = (decision.get("coverage_status") or "").lower()
    if not status:
        status = "covered" if decision.get("is_covered") else "excluded"
    score = decision.get("fraud_score") or 0.0

    if guardrail_flags:
        return "human_review", f"guardrail flag: {guardrail_flags[0]}"
    if doubts:
        return "human_review", f"coverage not established: {doubts[0]}"
    if payout_problems:
        return "human_review", f"the payout cannot be computed safely: {payout_problems[0]}"
    if status == "excluded":
        reasons = decision.get("coverage_reasons") or []
        return "reject", f"not covered: {reasons[0] if reasons else 'excluded'}"
    if score >= FRAUD_THRESHOLD:
        return "human_review", f"fraud score {score:.2f} >= {FRAUD_THRESHOLD}"
    if payout > HIGH_VALUE:
        return "human_review", f"payout above Rs {HIGH_VALUE:,}"
    return "auto_approve", "covered, low fraud risk, normal value"


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


def saved_state(claim_id: str) -> dict:
    """The last saved state for this claim, decided or waiting."""
    for folder in (DECISIONS, REVIEW_QUEUE):
        path = folder / f"{claim_id}.json"
        if path.exists():
            return json.loads(path.read_text())
    return {}


def resume_claim(claim_id: str, interactive: bool = True, verbose: bool = False) -> dict:
    """Continue a claim that was waiting, now that evidence has arrived.

    Only what the new evidence affects is run again. A value the claimant typed
    in (a missing total, say) is placed into the saved intake in code, so the
    Intake crew does not read the documents a second time and the assessment
    runs on the completed file. A new document does mean reading it again.

    Returns {"resumed": bool, "why": str, "state": dict|None}."""
    state = saved_state(claim_id)
    if not state:
        return {"resumed": False, "why": f"{claim_id} has not been through the Flow yet",
                "state": None}

    items = evidence.unapplied(claim_id)
    if not items:
        asked = [r.item for r in evidence.outstanding(state)]
        return {"resumed": False, "state": None,
                "why": ("no new evidence has been accepted for this claim; still waiting for: "
                        + ", ".join(asked) if asked
                        else "no new evidence, and nothing is outstanding")}

    steps = evidence.plan(state, items)
    intake = state.get("intake") or {}
    intake, changes = evidence.apply_to_intake(intake, items)
    if changes:
        INTAKE.mkdir(parents=True, exist_ok=True)
        (INTAKE / f"{claim_id}.json").write_text(json.dumps(intake, indent=2, default=str))

    print(f"\n  [{claim_id}] resuming with {len(items)} piece(s) of evidence")
    for line in changes + steps["why"]:
        print(f"      {line}")

    evidence.mark_applied(claim_id, items)
    flow = ClaimFlow()
    with observability.claim_span(claim_id, resumed=True):
        flow.kickoff(inputs={"claim_id": claim_id,
                             "use_saved_intake": not steps["run_intake"],
                             "interactive": interactive, "verbose": verbose,
                             "resumed": True, "evidence_applied": changes})
    return {"resumed": True, "why": "; ".join(steps["why"]), "state": flow.state.model_dump()}


def run_or_resume(claim_id: str, interactive: bool = True, verbose: bool = False,
                  use_saved_intake: bool = True) -> dict:
    """Assess a claim, or carry on a waiting claim if evidence has arrived.

    One entry point, so a claimant saying "assess my claim" gets the right
    behaviour whether it is the first time or after sending what was missing."""
    waiting = saved_state(claim_id).get("status") == "awaiting_evidence"
    if waiting and evidence.unapplied(claim_id):
        outcome = resume_claim(claim_id, interactive=interactive, verbose=verbose)
        if outcome["resumed"]:
            return outcome["state"]
    flow = ClaimFlow()
    # One span per claim, so the Flow, both crews, every LLM call and every
    # tool call appear under it as one tree in Langfuse.
    with observability.claim_span(claim_id):
        flow.kickoff(inputs={"claim_id": claim_id, "use_saved_intake": use_saved_intake,
                             "interactive": interactive, "verbose": verbose})
    return flow.state.model_dump()


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
        if agent_said != (not gaps) and not self.state.resumed:
            self.log(f"intake: Intake Officer said complete={agent_said}; "
                     f"the completeness check overrides it")
        self.state.intake["is_complete"] = not gaps
        self.state.intake["missing_fields"] = gaps
        lines = len(self.state.intake["invoice"]["line_items"])
        if self.state.resumed:
            self.log(f"intake: reused, with {len(self.state.evidence_applied)} "
                     f"field(s) filled in from new evidence", resumed=True)
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
        """Stop and name the exact evidence needed, rather than failing the claim.

        The claim keeps its state, so once the evidence arrives (add_evidence.py)
        resume_claim() carries on from here without reading the documents again."""
        gaps = self.state.intake.get("missing_fields") or ["details not stated"]
        self.state.route = "needs_evidence"
        self.state.status = "awaiting_evidence"
        for gap in gaps:
            field = evidence.field_for(gap)
            how = (f"send the value: uv run python add_evidence.py {self.state.claim_id} "
                   f"--item \"{gap}\" --value <value>") if field else \
                  (f"send the document: uv run python add_evidence.py {self.state.claim_id} "
                   f"--item \"{gap}\" --file <path>")
            self.log(f"evidence request: {gap}", kind="evidence_request", item=gap, how=how)
            print(f"      {how}")
        self.finish()

    @listen("complete")
    def assess(self):
        self.log("assessment: Policy Analyst and Fraud Analyst running in parallel")
        self.state.decision = self.guarded("assessment", run_assessment_step,
                                           assessment_inputs(self.state.intake), self.state.verbose)
        d = self.state.decision
        status = (d.get("coverage_status") or
                  ("covered" if d.get("is_covered") else "excluded")).lower()
        self.state.coverage_doubts = coverage_doubts(self.state.intake, d)
        self.state.payout_doubts = payout_doubts(self.state.intake)

        if status == "excluded" and not self.state.coverage_doubts:
            self.state.settlement = {"payout": 0.0, "explanation": "Not covered: no settlement."}
        else:
            # covered, or uncertain: the adjuster needs the figure the claim would pay
            self.state.settlement = settle(self.state.intake["policy_no"], d["line_decisions"])
            if status != "covered":
                self.state.settlement["explanation"] = (
                    "PROVISIONAL, subject to a person confirming coverage.\n"
                    + self.state.settlement.get("explanation", ""))
        denied = sum(not line["admitted"] for line in d["line_decisions"])
        self.log(f"assessment: coverage={status}, fraud_score={d['fraud_score']:.2f}, "
                 f"{denied} line(s) denied, payout Rs {self.state.settlement['payout']:,.2f} (calculator)",
                 coverage_status=status)
        for doubt in self.state.coverage_doubts:
            self.log(f"coverage doubt: {doubt}", kind="coverage_doubt")
        for doubt in self.state.payout_doubts:
            self.log(f"payout doubt: {doubt}", kind="payout_doubt")

    @router(assess, emit=["auto_approve", "human_review", "reject"])
    def route_claim(self):
        d, payout = self.state.decision, self.state.settlement["payout"]
        route, why = decide_route(d, self.state.settlement, self.state.guardrail_flags,
                                  self.state.coverage_doubts, self.state.payout_doubts)
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
        status = (d.get("coverage_status") or
                  ("covered" if d.get("is_covered") else "excluded")).lower()
        print(f"coverage      {status}")
        for reason in (d.get("coverage_reasons") or [])[:3]:
            print(f"              - {reason}")
        if self.state.coverage_doubts:
            print("NEEDS A DECISION ON COVERAGE")
            for doubt in self.state.coverage_doubts:
                print(f"              ! {doubt}")
        if self.state.payout_doubts:
            print("THE PAYOUT NEEDS CHECKING BY HAND")
            for doubt in self.state.payout_doubts:
                print(f"              ! {doubt}")
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
