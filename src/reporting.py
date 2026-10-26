"""
The claim evidence report: why ClaimSight decided what it decided.

Nothing in this module calls a language model. The report is assembled from
what was actually recorded while the claim ran:

    data/decisions/<id>.json     the Flow's final state
    data/review_queue/<id>.json  the same, for a claim still waiting on a person
    data/logs/<id>.jsonl         every tool call, task, guardrail and router event

So each statement in the report traces back to the tool result, policy clause
or document it came from, and the report can be rebuilt at any time without
spending a single token. Asking an LLM to explain a decision after the fact
would produce a plausible story rather than the real reasons.

Used by:
    show_report.py            print or save a claim's report
    src/mcp_server.py         the get_claim_report tool (PII redacted)
    src/a2a_server.py         a short explanation sent back with the decision
"""

import json
import re
import sys
from pathlib import Path
from typing import List, Optional

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
sys.path.insert(0, str(SRC))

import evidence                                                      # noqa: E402
from models import ClaimReport, EvidenceReference, EvidenceRequest   # noqa: E402

DECISIONS = ROOT / "data" / "decisions"
REVIEW_QUEUE = ROOT / "data" / "review_queue"
LOGS = ROOT / "data" / "logs"
REPORTS = ROOT / "data" / "reports"

# How a claim's money stands. A claim waiting on a person has no final figure
# at all, and showing one as nought would read as a refusal.
AUTHORISED = {"approved", "approved_by_adjuster"}
REFUSED = {"rejected", "rejected_by_adjuster"}
BY_A_PERSON = {"approved_by_adjuster", "rejected_by_adjuster"}
SETTLEMENT_STATE = {
    "pending_human_review": "PENDING HUMAN REVIEW - no payment authorised yet",
    "pending_evidence": "ON HOLD - waiting for documents from the claimant",
    "authorised": "AUTHORISED",
    "refused": "REFUSED - nothing payable",
    "not_assessed": "NOT ASSESSED",
}


def settlement_state(status: str) -> str:
    """Which of the five states this claim's money is in."""
    status = (status or "").strip()
    if status in AUTHORISED:
        return "authorised"
    if status in REFUSED:
        return "refused"
    if status == "awaiting_review":
        return "pending_human_review"
    if status == "awaiting_evidence":
        return "pending_evidence"
    return "not_assessed"


def money_line(status: str, final_payout: Optional[float],
               proposed: Optional[float] = None) -> str:
    """The claim's money in a few words, for a terminal summary.

    Only an authorised or refused claim has a final figure. A claim waiting
    for a person shows the calculator's proposal, labelled as one; a claim
    waiting for evidence has no figure at all. Rs 0.00 on either would read
    as a decision nobody made."""
    state = settlement_state(status)
    if state == "authorised":
        return f"Rs {float(final_payout or 0.0):,.2f}"
    if state == "refused":
        return "Rs 0.00 (refused)"
    if state == "pending_human_review":
        if proposed is None:
            return "none authorised (awaiting review)"
        return f"proposed Rs {float(proposed):,.2f}, not authorised (awaiting review)"
    if state == "pending_evidence":
        return "not assessed (awaiting evidence)"
    return "none (not assessed)"


def line_label(admitted: bool, state: str, decided_by_person: bool) -> str:
    """What to call one invoice line, given where the claim has got to.

    A line the agents would not admit is DISPUTED until a person has looked at
    it; only afterwards is it DENIED. The difference matters to a claimant who
    is reading why their claim is short."""
    if state in {"pending_human_review", "pending_evidence", "not_assessed"}:
        return "PROPOSE PAY" if admitted else "DISPUTED   "
    if state == "refused":
        return "NOT PAID   "
    if admitted:
        return "PAID       "
    return "DENIED     " if decided_by_person else "NOT PAID   "


# What each tool contributes, in words an assessor can read.
TOOL_MEANING = {
    "check_policy_period": "whether the incident fell inside the cover dates",
    "check_part_prices": "whether any billed part is above the market rate",
    "find_prior_claims": "whether the same damage was claimed before",
    "calculate_settlement": "the payout under the policy's depreciation rules",
    "get_claim_status": "where the claim stands",
}


def _tool_name(raw: str) -> str:
    """Strip the MCP launch prefix, so 'claimsight_tools_check_part_prices'
    reads as 'check_part_prices'."""
    name = (raw or "").strip()
    for known in TOOL_MEANING:
        if name.endswith(known):
            return known
    return name


# ---------------------------------------------------------------- loading
def load_state(claim_id: str) -> Optional[dict]:
    """The Flow's saved state for this claim, decided or still in the queue."""
    for folder in (DECISIONS, REVIEW_QUEUE):
        path = folder / f"{claim_id}.json"
        if path.exists():
            return json.loads(path.read_text())
    return None


def load_log(claim_id: str) -> List[dict]:
    """The claim's monitoring log, one dict per event. Missing log: no evidence
    trail, but the report still builds from the saved state."""
    path = LOGS / f"{claim_id}.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


# ---------------------------------------------------------------- building
# A coverage reason about the cover dates rather than a clause. The policy
# period is settled by the check_policy_period tool, not by the wording, so
# such a reason is checked against what that tool actually answered.
PERIOD_RE = re.compile(r"\b(period|policy term|cover(age)?\s+(dates|term|began|ended))\b", re.I)
OUTSIDE_RE = re.compile(r"\b(outside|expired|lapsed|not within|not inside|before cover|"
                        r"after cover|had ended|had not begun)\b", re.I)


def period_from_tool(log: List[dict]) -> Optional[bool]:
    """within_period as check_policy_period reported it, or None if it never ran."""
    for entry in log:
        if entry.get("event") != "tool_call":
            continue
        if _tool_name(entry.get("tool", "")) != "check_policy_period":
            continue
        try:
            answer = json.loads(entry.get("result", "") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(answer, dict) and "within_period" in answer:
            return bool(answer["within_period"])
    return None


def _coverage_references(decision: dict, log: List[dict]) -> List[EvidenceReference]:
    """Each coverage reason, with whether it is actually backed by something.

    A reason about a clause is checked against the policy wording, the same
    check the router uses before allowing a rejection. A reason about the cover
    dates is checked against the tool that computed them: if the agent says the
    incident was outside the period and the tool says it was inside, that
    disagreement is the useful thing to show."""
    import citations

    # The conclusion the reasons are supporting matters: Section I item 6 is a
    # correct citation for a covered claim and the wrong one for a refusal.
    status = str(decision.get("coverage_status") or "").strip().lower()
    if not status and "is_covered" in decision:
        status = "covered" if decision["is_covered"] else "excluded"

    within = period_from_tool(log)
    out = []
    for reason in (decision.get("coverage_reasons") or []):
        if not reason:
            continue
        verdict = citations.check(reason, verdict=status or "covered")
        if PERIOD_RE.search(reason) and not verdict["source_verified"]:
            agrees = None if within is None else (within is not bool(OUTSIDE_RE.search(reason)))
            out.append(EvidenceReference(
                kind="policy_clause", source="policy period (check_policy_period)",
                detail=reason, grounded=agrees))
        else:
            # Three findings, not one: named, found in the document, and
            # actually supporting the conclusion it is cited for.
            source = ("policy wording: " + verdict["note"]) if verdict["note"] \
                else "policy wording"
            out.append(EvidenceReference(
                kind="policy_clause", source=source, detail=reason,
                grounded=verdict["source_verified"] and verdict["supports_conclusion"]))
    return out


def _document_references(intake: dict, registration: Optional[str] = None) -> List[EvidenceReference]:
    invoice = intake.get("invoice") or {}
    registration = registration or invoice.get("registration") or intake.get("registration")
    lines = invoice.get("line_items") or []
    out = [EvidenceReference(
        kind="document", source="invoice",
        detail=f"{len(lines)} line(s), total Rs {float(invoice.get('total') or 0):,.2f}"
               f"{', invoice ' + str(invoice['invoice_no']) if invoice.get('invoice_no') else ''}")]
    if intake.get("policy_no"):
        out.append(EvidenceReference(
            kind="document", source="policy schedule",
            detail=f"{intake['policy_no']}, vehicle {registration or 'not stated'}"))
    if intake.get("narrative"):
        out.append(EvidenceReference(
            kind="document", source="claimant's account",
            detail=intake["narrative"][:300]))
    return out


def _log_references(log: List[dict]) -> List[EvidenceReference]:
    """Tool results, guardrail checks and each analyst's conclusion, in the
    order they happened."""
    out: List[EvidenceReference] = []
    for entry in log:
        event = entry.get("event")
        at = entry.get("elapsed_s")
        if event == "tool_call":
            name = _tool_name(entry.get("tool", ""))
            purpose = TOOL_MEANING.get(name)
            detail = f"asked: {entry.get('input', '')}\nanswered: {entry.get('result', '')}"
            if entry.get("tool_error"):
                detail = "TOOL ERROR -> " + detail
            out.append(EvidenceReference(
                kind="tool", source=name + (f" ({purpose})" if purpose else ""),
                detail=detail, at_seconds=at))
        elif event == "guardrail":
            passed = entry.get("passed")
            bits = {k: v for k, v in entry.items()
                    if k not in {"time", "elapsed_s", "claim_id", "event", "kind", "passed"}}
            out.append(EvidenceReference(
                kind="guardrail", source=entry.get("kind", "guardrail"),
                detail=("passed" if passed else "stopped the output") +
                       (f": {json.dumps(bits, default=str)[:300]}" if bits else ""),
                at_seconds=at))
        elif event == "task_done" and entry.get("agent"):
            # The Lead's task output IS the decision, and the report already
            # shows it field by field, so its raw JSON is not repeated here.
            if entry.get("structured"):
                continue
            out.append(EvidenceReference(
                kind="analysis", source=str(entry["agent"]),
                detail=str(entry.get("output", ""))[:400], at_seconds=at))
    return out


def _submitted_references(claim_id: str) -> List[EvidenceReference]:
    """Evidence the claimant sent in, with the verdict it was given. Rejected
    items stay on the record: what was turned away and why is part of the
    explanation a claimant is owed."""
    out = []
    for item in evidence.ledger(claim_id)["items"]:
        what = item.get("file") or item.get("value")
        out.append(EvidenceReference(
            kind="document", source=f"submitted evidence ({item['item']})",
            detail=f"{'accepted' if item.get('accepted') else 'REJECTED'}: {what} "
                   f"- {item.get('verdict', '')}"
                   + (" (applied to the claim)" if item.get("applied") else ""),
            grounded=bool(item.get("accepted"))))
    return out


def _route_reason(state: dict, log: List[dict]) -> str:
    """The router's own words for why this route was taken, e.g.
    "router 2: human_review because fraud score 0.90 >= 0.5" -> the part after
    "because"."""
    def tidy(message: str) -> str:
        message = str(message or "")
        if " because " in message:
            return message.split(" because ", 1)[1]
        return message.split(": ", 1)[-1]

    for entry in reversed(log):
        if entry.get("event") == "flow" and entry.get("router") == "route_claim":
            return tidy(entry.get("message"))
    for message in reversed(state.get("trail") or []):
        if message.startswith(("router 2:", "router 1:")):
            return tidy(message)
    return ""


def _run_facts(log: List[dict]) -> dict:
    """Seconds, tokens and tool calls, taken from the monitoring log. Useful on
    its own, and the starting point for the cost and time evaluation."""
    facts: dict = {}
    crews = {}
    for entry in log:
        if entry.get("event") == "crew_done":
            crews[entry.get("crew", "crew")] = {
                "seconds": entry.get("seconds"), "total_tokens": entry.get("total_tokens")}
        elif entry.get("event") == "run_finished":
            for key in ("total_seconds", "tool_calls", "tasks", "errors",
                        "retried_llm_failures"):
                if key in entry:
                    facts[key] = entry[key]
    if crews:
        facts["crews"] = crews
        tokens = [c["total_tokens"] for c in crews.values() if c.get("total_tokens")]
        if tokens:
            facts["total_tokens"] = sum(tokens)
    return facts


def build_report(state: dict, log: Optional[List[dict]] = None) -> ClaimReport:
    """Assemble the report. `state` is a saved ClaimState dump."""
    log = log if log is not None else load_log(state.get("claim_id", ""))
    intake = state.get("intake") or {}
    decision = state.get("decision") or {}
    settlement = state.get("settlement") or {}
    invoice = intake.get("invoice") or {}

    # An empty coverage_status means the claim never reached assessment. Only
    # fall back to the old boolean when the decision actually carries one, so a
    # claim still waiting on documents never reads as "not covered".
    status = str(decision.get("coverage_status") or "").strip().lower()
    if not status and "is_covered" in decision:
        status = "covered" if decision["is_covered"] else "excluded"

    return ClaimReport(
        claim_id=state.get("claim_id", ""),
        policy_no=intake.get("policy_no", ""),
        incident_date=intake.get("incident_date"),
        registration=invoice.get("registration") or intake.get("registration"),
        coverage_status=status,
        coverage_reasons=_coverage_references(decision, log),
        coverage_doubts=state.get("coverage_doubts") or [],
        payout_doubts=state.get("payout_doubts") or [],
        fraud_score=float(decision.get("fraud_score") or 0.0),
        fraud_flags=list(decision.get("fraud_flags") or []),
        line_decisions=decision.get("line_decisions") or [],
        settlement_explanation=settlement.get("explanation", ""),
        proposed_payout=float(settlement.get("payout") or 0.0),
        settlement_state=settlement_state(state.get("status")),
        authorised_payout=(float(state.get("final_payout") or 0.0)
                           if settlement_state(state.get("status"))
                           in {"authorised", "refused"} else None),
        final_payout=float(state.get("final_payout") or 0.0),
        evidence=_document_references(
            intake, invoice.get("registration") or intake.get("registration")
        ) + _submitted_references(state.get("claim_id", "")) + _log_references(log),
        outstanding_evidence=evidence.outstanding(state),
        route=state.get("route", ""),
        status=state.get("status", ""),
        route_reason=_route_reason(state, log),
        guardrail_flags=list(state.get("guardrail_flags") or []),
        run=_run_facts(log),
    )


def for_claim(claim_id: str) -> Optional[ClaimReport]:
    """The report for a claim that has already been through the Flow."""
    state = load_state(claim_id)
    return build_report(state) if state else None


# ---------------------------------------------------------------- rendering
BAR = "=" * 74


def _money(value: float) -> str:
    return f"Rs {value:,.2f}"


def _check_mark(ref: EvidenceReference) -> str:
    """How a coverage reason was checked, so a reader can see at a glance which
    findings rest on the policy document and which are the agent's own words."""
    by_tool = "policy period" in ref.source
    note = (ref.source or "").lower()
    if ref.grounded is None:
        return "not checked"
    if ref.grounded:
        return "tool verified" if by_tool else "clause verified"
    if by_tool:
        return "DISAGREES WITH TOOL"
    if "not in this policy" in note or "not in section" in note:
        return "CLAUSE NOT IN POLICY"
    if "covers" in note:
        return "CITES COVER, NOT AN EXCLUSION"
    if "no clause or quotation" in note:
        return "NO CLAUSE CITED"
    return "NOT SUPPORTED BY THE CLAUSE"


def render(report: ClaimReport) -> str:
    """The report as terminal text, in the order an assessor reads it."""
    r = report
    out = [BAR, f"CLAIM EVIDENCE REPORT   {r.claim_id}", BAR,
           f"Policy {r.policy_no or 'not stated'}   incident {r.incident_date or 'not stated'}"
           f"   vehicle {r.registration or 'not stated'}", ""]

    out.append(f"COVERAGE: {r.coverage_status.upper() or 'NOT ASSESSED'}")
    for ref in r.coverage_reasons:
        out.append(f"  [{_check_mark(ref)}] {ref.detail}")
    if not r.coverage_reasons:
        out.append("  (no coverage reasons recorded)")
    for doubt in r.coverage_doubts:
        out.append(f"  ! {doubt}")
    out.append("")

    out.append(f"FRAUD SCORE: {r.fraud_score:.2f}")
    for flag in r.fraud_flags:
        out.append(f"  - {flag}")
    if not r.fraud_flags:
        out.append("  - none")
    for flag in r.guardrail_flags:
        out.append(f"  ! guardrail: {flag}")
    out.append("")

    by_person = r.status in BY_A_PERSON
    pending = r.settlement_state in {"pending_human_review", "pending_evidence",
                                     "not_assessed"}
    out.append("INVOICE LINES" + ("   (nothing here is final yet)" if pending else ""))
    for line in r.line_decisions:
        mark = line_label(line.admitted, r.settlement_state, by_person)
        note = f"   {line.reason}" if line.reason else ""
        out.append(f"  {mark} {line.description}: {_money(line.amount)}{note}")
    if not r.line_decisions:
        out.append("  (none)")
    out.append("")

    out.append("SETTLEMENT (computed in code, not by a model)")
    for text in (r.settlement_explanation or "not calculated").splitlines():
        out.append(f"  {text}")
    for doubt in r.payout_doubts:
        out.append(f"  ! {doubt}")
    out.append(f"  Assessed as worth:  {_money(r.proposed_payout)}")
    out.append(f"  Status:             {SETTLEMENT_STATE[r.settlement_state]}")
    if r.authorised_payout is not None:
        out.append(f"  Authorised payout:  {_money(r.authorised_payout)}")
        if (by_person and r.proposed_payout
                and abs(r.authorised_payout - r.proposed_payout) > 0.01):
            out.append(f"  (the adjuster authorised a different amount from the "
                       f"assessed {_money(r.proposed_payout)})")
    out.append("")

    if r.outstanding_evidence:
        out.append("STILL NEEDED")
        for request in r.outstanding_evidence:
            out.append(f"  from the {request.from_whom}: {request.item}")
            out.append(f"      because {request.why}")
        out.append("")

    out.append("EVIDENCE USED")
    for ref in r.evidence:
        when = f"  [{ref.at_seconds:>6.2f}s]" if ref.at_seconds is not None else "  [       ]"
        out.append(f"{when} {ref.kind}: {ref.source}")
        for text in ref.detail.splitlines():
            out.append(f"            {text}")
    if not r.evidence:
        out.append("  (no monitoring log found for this claim)")
    out.append("")

    out.append(f"OUTCOME: {r.status or 'not decided'}   route {r.route or 'none'}")
    if r.route_reason:
        out.append(f"  because {r.route_reason}")
    if r.run:
        bits = [f"{r.run.get('total_seconds', '?')}s"]
        if r.run.get("total_tokens"):
            bits.append(f"{r.run['total_tokens']:,} tokens")
        if r.run.get("tool_calls") is not None:
            bits.append(f"{r.run['tool_calls']} tool calls")
        if r.run.get("retried_llm_failures"):
            bits.append(f"{r.run['retried_llm_failures']} retried LLM failures")
        out.append("  run: " + ", ".join(str(b) for b in bits))
    out.append(BAR)
    return "\n".join(out)


def to_markdown(report: ClaimReport) -> str:
    """The same report as Markdown, for an assessor to read outside a terminal."""
    r = report
    out = [f"# Claim evidence report: {r.claim_id}", "",
           f"- **Policy:** {r.policy_no or 'not stated'}",
           f"- **Incident date:** {r.incident_date or 'not stated'}",
           f"- **Vehicle:** {r.registration or 'not stated'}",
           f"- **Outcome:** {r.status or 'not decided'} (route `{r.route or 'none'}`)",
           f"- **Settlement:** {SETTLEMENT_STATE[r.settlement_state]}"]
    if r.route_reason:
        out.append(f"- **Why:** {r.route_reason}")
    out += ["", f"## Coverage: {r.coverage_status or 'not assessed'}", ""]
    for ref in r.coverage_reasons:
        mark = _check_mark(ref)
        label = mark if ref.grounded else f"**{mark}**"
        out.append(f"- {label}: {ref.detail}")
    for doubt in r.coverage_doubts:
        out.append(f"- **Doubt:** {doubt}")
    out += ["", f"## Fraud score: {r.fraud_score:.2f}", ""]
    out += [f"- {flag}" for flag in r.fraud_flags] or ["- none"]
    out += ["", "## Invoice lines", ""]
    if r.settlement_state in {"pending_human_review", "pending_evidence", "not_assessed"}:
        out.append("Nothing in this table is final: the claim has not been decided yet.\n")
    out += ["| Line | Amount | Decision | Reason |", "|---|---|---|---|"]
    by_person = r.status in BY_A_PERSON
    for line in r.line_decisions:
        out.append(f"| {line.description} | {_money(line.amount)} | "
                   f"{line_label(line.admitted, r.settlement_state, by_person).strip().lower()} | "
                   f"{line.reason or ''} |")
    out += ["", "## Settlement", "", "```", r.settlement_explanation or "not calculated",
            f"Assessed as worth: {_money(r.proposed_payout)}",
            f"Status:            {SETTLEMENT_STATE[r.settlement_state]}"]
    if r.authorised_payout is not None:
        out.append(f"Authorised payout: {_money(r.authorised_payout)}")
    out.append("```")
    if r.outstanding_evidence:
        out += ["", "## Still needed", ""]
        out += [f"- From the {q.from_whom}: **{q.item}** — because {q.why}"
                for q in r.outstanding_evidence]
    out += ["", "## Evidence used", ""]
    for ref in r.evidence:
        when = f"{ref.at_seconds:.2f}s" if ref.at_seconds is not None else "-"
        detail = ref.detail.replace("\n", "<br>")
        out.append(f"- `{when}` **{ref.kind}** · {ref.source}: {detail}")
    if r.run:
        out += ["", "## This run", "", "```", json.dumps(r.run, indent=2), "```"]
    return "\n".join(out) + "\n"


def explanation(report: ClaimReport) -> str:
    """Two to four sentences a claimant-facing agent can relay. Assembled from
    the recorded findings, so it says only what the assessment actually found."""
    r = report
    parts = []
    if r.coverage_status == "covered":
        parts.append("The policy covers this incident"
                     + (f" ({r.coverage_reasons[0].detail})" if r.coverage_reasons else "") + ".")
    elif r.coverage_status == "excluded":
        parts.append("The policy does not cover this incident"
                     + (f": {r.coverage_reasons[0].detail}" if r.coverage_reasons else "") + ".")
    elif r.coverage_status == "uncertain":
        parts.append("The policy wording did not settle whether this incident is covered, "
                     "so a claims officer will decide.")

    pending = r.settlement_state in {"pending_human_review", "pending_evidence",
                                     "not_assessed"}
    denied = [line for line in r.line_decisions if not line.admitted]
    if denied:
        lead = ("Questioned, and not yet decided: " if pending else "Not paid: ")
        parts.append(lead + "; ".join(
            f"{line.description} ({line.reason or 'no reason recorded'})" for line in denied) + ".")
    if r.fraud_flags:
        parts.append("Checks raised: " + "; ".join(r.fraud_flags[:2]) + ".")

    claimant_asks = [q for q in r.outstanding_evidence if q.from_whom == "claimant"]
    if claimant_asks:
        parts.append("Still needed from you: "
                     + ", ".join(q.item for q in claimant_asks) + ".")

    if r.status == "approved":
        parts.append(f"Approved, and {_money(r.authorised_payout or r.final_payout)} "
                     f"is authorised for payment.")
    elif r.status == "approved_by_adjuster":
        parts.append(f"A claims adjuster reviewed it and authorised "
                     f"{_money(r.authorised_payout or r.final_payout)}.")
    elif r.status == "rejected_by_adjuster":
        parts.append("A claims adjuster reviewed it and refused the claim, "
                     "so nothing is payable.")
    elif r.status == "awaiting_review":
        parts.append(f"No payment has been authorised yet: a claims adjuster is still "
                     f"reviewing the claim. As assessed it would come to "
                     f"{_money(r.proposed_payout)}, which may change.")
    elif r.status == "rejected":
        parts.append("The claim was refused, so nothing is payable.")
    elif r.status == "awaiting_evidence":
        parts.append("The claim is on hold until the missing documents arrive. "
                     "Nothing has been decided and no amount has been refused.")
    return " ".join(parts)


def save(report: ClaimReport, markdown: bool = False) -> Path:
    """Write the report to data/reports/. Returns the path written."""
    REPORTS.mkdir(parents=True, exist_ok=True)
    if markdown:
        path = REPORTS / f"{report.claim_id}.md"
        path.write_text(to_markdown(report))
    else:
        path = REPORTS / f"{report.claim_id}.json"
        path.write_text(json.dumps(report.model_dump(), indent=2, default=str))
    return path
