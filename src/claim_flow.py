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
from typing import Dict, List, Optional, Tuple

from crewai.flow.flow import Flow, listen, router, start
from crewai.flow.persistence import persist
from pydantic import BaseModel, Field

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

import citations                             # noqa: E402
import clause_scope                          # noqa: E402
import evidence                              # noqa: E402
import guardrails                            # noqa: E402
import observability                         # noqa: E402
import monitoring                            # noqa: E402
from tools.assessment_tools import (                             # noqa: E402
    check_billed_parts_against_photo,
    check_policy_period,
    settle,
)
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
    # The photograph against the invoice, checked in code after intake.
    photo_check: Dict = Field(default_factory=dict)
    photo_doubts: List[str] = Field(default_factory=list)
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


def run_assessment_step(inputs: dict, verbose: bool, build=None) -> dict:
    """`build` makes the crew. The evaluation passes a single-LLM crew here, so
    both configurations are run, timed and counted by this same code."""
    if build is None:
        from crews.assessment_crew import build_assessment_crew as build
    monitor = monitoring.current()
    started = time.time()
    output = build(verbose=verbose, monitor=monitor).kickoff(inputs=inputs)
    usage = getattr(output, "token_usage", None)
    if monitor:
        # Everything the provider reported, not just the total; see
        # monitoring.USAGE_FIELDS. No cost is estimated here.
        monitor.record("crew_done", crew="assessment",
                       seconds=round(time.time() - started, 2),
                       **monitoring.usage_fields(usage),
                       retried_llm_failures=monitor.counts.get("retried_llm_failures", 0),
                       llm_errors=sorted(monitor.llm_error_kinds))
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


CLEARER_PHOTO = "a clearer photograph of the damage"


def photo_request(part: str) -> str:
    return f"photograph showing the {part}"


def check_photos(intake: dict, supplied: list = ()) -> dict:
    """Compare the parts billed with what the Damage Analyst saw, in code.

    {} when the claim has no photo findings: nothing to compare. Otherwise the
    result of check_billed_parts_against_photo, plus what follows from it:

      flags       a billed part the photograph shows UNDAMAGED. Positive
                  evidence against the line, so a person must look.
      requests    a billed part the photograph does not show. Not evidence of
                  anything, so it is not a flag: the claimant is asked for a
                  photograph of it, through the ordinary evidence loop.
      supplied    a requested photograph that has arrived. A claimant's photo
                  is not sent to a cloud model (vision.photo_clearance), so
                  nothing has examined it: a person must, before payment.

    `supplied` is the evidence items already accepted for this claim.

    Several photographs are read together: a part seen damaged in any of them
    is damaged; seen intact in one and not damaged in any, undamaged."""
    findings = [p for p in (intake.get("photo_damage") or []) if isinstance(p, dict)]
    if not findings:
        return {}
    readable = [p for p in findings if p.get("readable")]
    damaged = {x for p in readable for x in p.get("damaged") or []}
    undamaged = {x for p in readable for x in p.get("undamaged") or []} - damaged
    not_visible = {x for p in findings for x in p.get("not_visible") or []} - damaged - undamaged
    lines = [{"description": l.get("description", ""), "amount": l.get("amount")}
             for l in ((intake.get("invoice") or {}).get("line_items") or [])]
    result = check_billed_parts_against_photo(
        lines, sorted(damaged), sorted(undamaged), sorted(not_visible),
        photo_readable=bool(readable))
    if result.get("error"):
        return {**result, "photos": [p.get("photo") for p in findings],
                "requests": [], "supplied": []}

    def sent(item):
        return any(evidence.same_item(item, done) for done in supplied)

    wanted = ([CLEARER_PHOTO] if not readable and result.get("billed_parts") else
              [photo_request(part) for part in result.get("unchecked_parts") or []])
    return {**result, "photos": [p.get("photo") for p in findings],
            "requests": [w for w in wanted if not sent(w)],
            "supplied": [w for w in wanted if sent(w)]}


def photo_doubts(check: dict) -> list:
    """Why the photograph means a person must decide, in the router's words."""
    out = [f"the photograph shows the {part} undamaged, but it is billed "
           f"({line})" for part, line in zip(check.get("unsupported_parts") or [],
                                             check.get("unsupported_lines") or [])]
    out += [f"the claimant sent the {item}; claimant photographs are not sent to a model, "
            f"so a person must inspect it" for item in check.get("supplied") or []]
    return out


def cites_policy(reason: str) -> bool:
    """Is this coverage reason grounded in the policy document?

    Delegates to src/citations.py, which looks the clause up in the wording
    instead of accepting anything clause-shaped. "Section II exclusion 4" is
    clause-shaped and this policy has no numbered exclusions at all, so it
    comes back False."""
    return citations.cites_policy(reason)


def _policy_record(policy_no) -> dict:
    """The policy schedule, for the add-ons it records as purchased. {} if absent."""
    path = POLICIES / f"{policy_no}.json"
    try:
        return json.loads(path.read_text()) if policy_no and path.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def submitted_incident_date(claim_id: Optional[str]) -> Optional[str]:
    """The incident date as the claimant SUBMITTED it (data/claims/<id>.json).

    The intake's incident_date is the Intake Officer's copy of this, made by a
    model; the period decision must not rest on a model's transcription."""
    path = CLAIMS / f"{claim_id}.json"
    try:
        return json.loads(path.read_text()).get("incident_date") if claim_id and path.exists() else None
    except (json.JSONDecodeError, OSError):
        return None


def policy_period(intake: dict) -> dict:
    """check_policy_period, run in code on the submitted date (the intake's copy
    only when the claim file has none, e.g. a date supplied later as evidence)."""
    date = submitted_incident_date(intake.get("claim_id")) or intake.get("incident_date")
    return check_policy_period(intake.get("policy_no") or "", date)


PERIOD_DENIAL = "outside the policy period (checked in code)"


def enforce_policy_period(intake: dict, decision: dict) -> Tuple[dict, Optional[dict]]:
    """An incident outside the cover dates cannot be covered, whatever a model says.

    Runs AFTER the crew, on the decision it returned. The Policy Analyst calls
    the same tool, but its answer passes through two models before it reaches the
    router, and on 18 Nov a guardrail retry loop turned a correct rejection of
    CLM-5006 (37 days before cover) into "covered" on a sentence the policy does
    not contain. So the tool's answer is enforced here, where no model is involved.

    Only a definite within_period=False acts. A tool error (no policy on file, an
    unreadable date) is not knowledge that the claim is out of cover, and changes
    nothing. Returns (decision, override); override is None when nothing changed
    the verdict, otherwise it records what the model had said."""
    period = policy_period(intake)
    if period.get("within_period") is not False:
        return decision, None
    before = (decision.get("coverage_status") or
              ("covered" if decision.get("is_covered") else "excluded")).lower()
    reason = (f"Policy period: the incident on {period['incident_date']} is outside the "
              f"cover from {period['policy_start']} to {period['policy_end']}"
              f"{' (' + period['note'] + ')' if period.get('note') else ''}. "
              f"Checked in code by check_policy_period.")
    enforced = {
        **decision,
        "coverage_status": "excluded",
        "is_covered": False,
        "coverage_reasons": [reason],
        "uncertainty_reasons": [],
        "line_decisions": [{**line, "admitted": False, "reason": PERIOD_DENIAL}
                           for line in decision.get("line_decisions") or []],
        "recommended_route": "reject",
    }
    if before == "excluded":
        return enforced, None              # the model already said so; nothing overridden
    override = {"model_coverage_status": before,
                "model_coverage_reasons": list(decision.get("coverage_reasons") or []),
                "period": period}
    enforced["period_override"] = override
    return enforced, override


# CrewAI raises a plain Exception with this wording when a task's guardrail has
# rejected the first output and every retry ("Task failed guardrail validation
# after 2 retries. Last error: ...").
GUARDRAIL_EXHAUSTED = re.compile(r"Task failed .*validation after \d+ retries")


def guardrail_exhausted(exc: BaseException) -> bool:
    return bool(GUARDRAIL_EXHAUSTED.search(str(exc)))


def exhausted_decision(intake: dict, exc: BaseException) -> Tuple[dict, str]:
    """The decision record for a claim no agent could produce a valid decision for.

    It decides nothing: coverage is "uncertain", every line is provisionally
    admitted so the adjuster sees what the claim would pay, and the fraud risk is
    marked as NOT assessed (the score field holds 0.0 only because it is numeric).
    Returns (decision, the reason a person must look)."""
    last = str(exc).split("Last error:", 1)[-1].strip() if "Last error:" in str(exc) else str(exc)
    why = (f"the Assessment crew could not produce a decision that passed validation "
           f"after every retry. Last problem: {last[:300]}")
    lines = (intake.get("invoice") or {}).get("line_items") or []
    return {
        "claim_id": intake.get("claim_id"),
        "coverage_status": "uncertain",
        "is_covered": False,
        "coverage_reasons": [],
        "uncertainty_reasons": [why],
        "fraud_score": 0.0,
        "fraud_flags": ["fraud risk NOT assessed: the assessment did not complete"],
        "line_decisions": [{"description": l.get("description"), "amount": l.get("amount"),
                            "material": l.get("material"), "admitted": True,
                            "reason": "provisional: not assessed"} for l in lines],
        "recommended_route": "human_review",
        "summary": "No valid decision was produced; a person must assess this claim.",
        "guardrail_exhausted": True,
    }, why


def coverage_doubts(intake: dict, decision: dict) -> list:
    """Reasons NOT to act on the coverage verdict without a person.

    Checked in code, like completeness_gaps, because an LLM contributes to the
    coverage decision and a wrong rejection costs the claimant their payout.
    An exclusion the policy period proves is objective and needs no clause."""
    status = (decision.get("coverage_status") or "").lower()
    if not status:                      # a decision saved before this field existed
        status = "covered" if decision.get("is_covered") else "excluded"
    reasons = [r for r in (decision.get("coverage_reasons") or []) if r]
    policy = _policy_record(intake.get("policy_no"))

    if status == "uncertain":
        said = (decision.get("uncertainty_reasons") or ["no reason given"])[0]
        return [f"the Policy Analyst could not establish coverage: {said}"]
    if status != "excluded":
        # A covered claim can still be cut short line by line. A line denied on
        # a clause from the scoped set (an endorsement or add-on whose heading
        # limits who it applies to) goes to a person: that is the IMT 21 error,
        # a commercial-vehicle exclusion applied to a private car. Lines denied
        # on base-policy clauses, prices or duplicates are not escalated here.
        doubts = []
        for line in decision.get("line_decisions") or []:
            if line.get("admitted") is False:
                cited = clause_scope.cited_blocks(line.get("reason") or "")
                if cited:
                    why = (clause_scope.inapplicable(line.get("reason") or "", policy)
                           or [f"{cited[0]} is an endorsement or add-on with a limited scope"])
                    doubts.append(f"'{line.get('description')}' was denied on {cited[0]}: {why[0]}")
        return doubts

    # Outside the cover dates is arithmetic, not judgement: the tool settles it
    # and no clause is needed.
    period = policy_period(intake)
    if period.get("within_period") is False:
        return []

    if not reasons:
        return ["the claim is refused with no reason given"]

    # A rejection needs a clause that exists AND that takes cover away. A
    # verified clause listing what the policy covers argues the other way.
    notes = []
    for reason in reasons:
        verdict = citations.check(reason, verdict="excluded", policy=policy)
        if verdict["source_verified"] and verdict["supports_conclusion"]:
            return []
        notes.append(verdict["note"])
    return [f"no reason supports refusing this claim: {notes[0]}"]


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
                 doubts: list, payout_problems: list = (), photo_problems: list = ()) -> tuple:
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
    if photo_problems:
        return "human_review", f"photo check: {photo_problems[0]}"
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
        # policy_form first: the analyst must know what class of policy it is
        # judging, since the wording also carries commercial-vehicle endorsements
        "policy": json.dumps({"policy_form": clause_scope.policy_form(policy),
                              **{k: policy[k] for k in POLICY_FIELDS}}, indent=1),
    }


FINAL_STATUSES = {"approved", "rejected", "awaiting_evidence", "awaiting_review",
                  "approved_by_adjuster", "rejected_by_adjuster"}


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
        blocked = evidence.stuck(claim_id)
        if blocked:
            return {"resumed": False, "state": None,
                    "why": (f"{len(blocked)} piece(s) of evidence failed "
                            f"{evidence.MAX_ATTEMPTS} times and need a person: "
                            f"{blocked[0].get('last_error', 'no reason recorded')}")}
        return {"resumed": False, "state": None,
                "why": ("no new evidence has been accepted for this claim; still waiting for: "
                        + ", ".join(asked) if asked
                        else "no new evidence, and nothing is outstanding")}

    steps = evidence.plan(state, items)
    intake_path = INTAKE / f"{claim_id}.json"
    before = intake_path.read_text() if intake_path.exists() else None

    intake = json.loads(json.dumps(state.get("intake") or {}))     # work on a copy
    intake, changes = evidence.apply_to_intake(intake, items)

    print(f"\n  [{claim_id}] resuming with {len(items)} piece(s) of evidence")
    for line in changes + steps["why"]:
        print(f"      {line}")

    # The evidence is marked "processing", not "applied": it is only spent once
    # the Flow has produced a decision and saved it. A run that fails here must
    # leave the claim exactly as it was, with the evidence still owed a
    # reassessment, rather than losing it or leaving a half-patched file.
    evidence.mark_processing(claim_id, items)
    if changes:
        evidence.write_json(intake_path, intake)

    flow = ClaimFlow()
    try:
        with observability.claim_span(claim_id, resumed=True):
            flow.kickoff(inputs={"claim_id": claim_id,
                                 "use_saved_intake": not steps["run_intake"],
                                 "interactive": interactive, "verbose": verbose,
                                 "resumed": True, "evidence_applied": changes})
        if flow.state.status not in FINAL_STATUSES:
            raise RuntimeError(f"the Flow stopped without a decision "
                               f"(status {flow.state.status or 'none'})")
    except Exception as exc:                                        # noqa: BLE001
        reason = str(exc).splitlines()[0][:200] or type(exc).__name__
        if before is None:
            intake_path.unlink(missing_ok=True)
        else:
            evidence.write_json(intake_path, json.loads(before))
        evidence.mark_pending(claim_id, items, error=reason)
        print(f"  [{claim_id}] resume failed: {reason}")
        print(f"      the evidence is still on file; try again with "
              f"'uv run python add_evidence.py {claim_id} --resume'")
        return {"resumed": False, "state": None,
                "why": f"the reassessment failed and nothing was changed: {reason}"}

    evidence.mark_applied(claim_id, items)
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
    # tool call in this process appear under it as one tree in Langfuse.
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

    def guarded(self, step: str, fn, *args, handled=None):
        """Run a crew step; log any error with where it happened, then re-raise.

        `handled` names errors the caller will deal with and carry on from (the
        run is not over, so the log is not finished as failed)."""
        try:
            return fn(*args)
        except Exception as exc:
            monitor = monitoring.current()
            if monitor:
                monitor.error(step, exc)
                if not (handled and handled(exc)):
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
        # Fresh log: data/logs/<claim>.jsonl. The model goes in it, so a saved
        # log says what produced it rather than leaving that to be assumed.
        import config
        monitoring.ClaimMonitor(self.state.claim_id, provider=config.PROVIDER,
                                model=config.PROFILES[config.PROVIDER]["text"])
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
            monitor = monitoring.current()
            monitor.record("crew_done", crew="intake",
                           seconds=round(time.time() - started, 2),
                           **monitor.crew_usage.get("intake", {}))
            evidence.write_json(saved, self.state.intake)

        # The agents only ever see the screened narrative, even from a saved intake
        self.state.intake["narrative"], _ = guardrails.safe_narrative(
            self.state.intake.get("narrative") or "")

        gaps = completeness_gaps(self.state.intake)
        agent_said = self.state.intake.get("is_complete")
        if agent_said != (not gaps) and not self.state.resumed:
            self.log(f"intake: Intake Officer said complete={agent_said}; "
                     f"the completeness check overrides it")

        # The photograph against the invoice. Run on every pass, a reused
        # intake included, so a resumed claim is checked against what has
        # arrived since.
        supplied = [item["item"] for item in evidence.accepted(self.state.claim_id)]
        self.state.photo_check = check_photos(self.state.intake, supplied)
        self.state.photo_doubts = photo_doubts(self.state.photo_check)
        if self.state.photo_check:
            c = self.state.photo_check
            self.log(f"photo check: {c.get('note') or c.get('error')}", kind="photo_check",
                     flagged=c.get("unsupported_parts") or [],
                     unchecked=c.get("unchecked_parts") or [],
                     requests=c.get("requests") or [], supplied=c.get("supplied") or [])
            gaps = gaps + list(c.get("requests") or [])
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
        try:
            self.state.decision = self.guarded("assessment", run_assessment_step,
                                               assessment_inputs(self.state.intake),
                                               self.state.verbose, handled=guardrail_exhausted)
        except Exception as exc:
            if not guardrail_exhausted(exc):
                raise                      # infrastructure and other errors fail as before
            # No agent produced a decision the guardrail would accept. That is a
            # claim a person must see, not a failure: escalate with the reason.
            self.state.decision, why = exhausted_decision(self.state.intake, exc)
            self.log(f"assessment: guardrail exhausted; escalating to a person: {why}",
                     kind="guardrail_exhausted")
            if policy_period(self.state.intake).get("within_period") is not False:
                self.state.guardrail_flags.append(why)
            # (outside the cover dates the claim is rejected below in code, which
            # needs no model decision at all)

        # The cover dates are enforced in code, after every model has spoken.
        self.state.decision, override = enforce_policy_period(self.state.intake,
                                                              self.state.decision)
        if override:
            self.log(f"policy period enforced in code: the model said "
                     f"'{override['model_coverage_status']}', but the incident is outside "
                     f"the cover dates ({override['period'].get('note', '')}); coverage set "
                     f"to excluded", kind="period_override")
        d = self.state.decision
        status = (d.get("coverage_status") or
                  ("covered" if d.get("is_covered") else "excluded")).lower()
        self.state.coverage_doubts = coverage_doubts(self.state.intake, d)
        self.state.payout_doubts = payout_doubts(self.state.intake)
        # A part the photograph shows intact is a fraud signal found by code,
        # not by the Fraud Analyst: it is added to the decision's flags so the
        # report carries it, and the router acts on it, whatever the score.
        flags = d.setdefault("fraud_flags", [])
        for flag in self.state.photo_check.get("flags") or []:
            flag = f"{flag} (photo check, in code)"
            if flag not in flags:
                flags.append(flag)

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
                                  self.state.coverage_doubts, self.state.payout_doubts,
                                  self.state.photo_doubts)
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
            evidence.write_json(REVIEW_QUEUE / f"{self.state.claim_id}.json",
                                self.state.model_dump())
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
        evidence.write_json(DECISIONS / f"{self.state.claim_id}.json",
                            self.state.model_dump())
