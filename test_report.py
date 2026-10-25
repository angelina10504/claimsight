"""
Test the claim evidence report.

Costs no tokens: the report is assembled from a claim's saved state and its
monitoring log, so this writes both into a temporary folder and checks what
comes out. What is being tested is that the report states the recorded
reasons and nothing else, and that it never leaks personal data.

    a finding traces back to the tool result behind it
    a coverage reason is marked cited or not cited
    an unsettled coverage question asks a person, not the claimant
    missing documents are asked of the claimant
    the claimant-facing explanation says what was actually found
    a phone number in the file never reaches the report over MCP

Run:  uv run python test_report.py
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

import reporting                        # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="claimsight_report_"))
reporting.DECISIONS = TMP / "decisions"
reporting.REVIEW_QUEUE = TMP / "review_queue"
reporting.LOGS = TMP / "logs"
reporting.REPORTS = TMP / "reports"
for folder in (reporting.DECISIONS, reporting.REVIEW_QUEUE, reporting.LOGS):
    folder.mkdir(parents=True, exist_ok=True)

LINES = [
    {"description": "Windshield - replacement", "amount": 31956.0, "material": "glass",
     "admitted": False, "reason": "priced far above market rate"},
    {"description": "Labour charges", "amount": 4790.0, "material": "labour",
     "admitted": True, "reason": ""},
]
NARRATIVE_WITH_PII = ("A stone hit the windshield on the highway. "
                      "Call me on 9876543210 or ravi.kumar@example.com.")


def state(claim_id, coverage_status, reasons, status, route, *, lines=None, doubts=(),
          uncertainty=(), missing=(), payout=16081.97, final=0.0, narrative="Stone impact.",
          assessed=True):
    """A saved ClaimState, as the Flow writes it. assessed=False is a claim that
    stopped at the completeness gate, so it has no decision at all."""
    built = {
        "claim_id": claim_id,
        "intake": {"claim_id": claim_id, "policy_no": "POL-3004",
                   "incident_date": "2026-05-30", "narrative": narrative,
                   "registration": "HR18DS4456", "missing_fields": list(missing),
                   "is_complete": not missing,
                   "invoice": {"invoice_no": "INV-77", "registration": "HR18DS4456",
                               "total": 56423.0,
                               "line_items": [{"description": x["description"],
                                               "amount": x["amount"],
                                               "material": x["material"]}
                                              for x in (lines if lines is not None else LINES)]}},
        "decision": {"claim_id": claim_id, "coverage_status": coverage_status,
                     "coverage_reasons": list(reasons), "uncertainty_reasons": list(uncertainty),
                     "fraud_score": 0.7, "fraud_flags": ["windshield priced far above market"],
                     "line_decisions": lines if lines is not None else LINES,
                     "recommended_route": route, "summary": "test"},
        "settlement": {"payout": payout, "explanation": f"Payout: Rs {payout:,.2f}"},
        "route": route, "status": status, "final_payout": final,
        "guardrail_flags": [], "coverage_doubts": list(doubts),
        "trail": [f"router 2: {route} because fraud score 0.70 >= 0.5"],
    }
    if not assessed:
        built["decision"], built["settlement"] = {}, {}
        built["trail"] = [f"router 1: file incomplete -> evidence request {list(missing)}"]
    return built


LOG = [
    {"time": "t", "elapsed_s": 0.0, "event": "run_started"},
    {"time": "t", "elapsed_s": 0.1, "event": "guardrail", "kind": "input_screening",
     "passed": True, "verdict": "clean"},
    {"time": "t", "elapsed_s": 3.4, "event": "tool_call",
     "tool": "claimsight_tools_check_part_prices",
     "input": '{"line_items": [{"description": "Windshield - replacement", "amount": 31956}]}',
     "result": '{"flags": [{"line": "Windshield - replacement", "market": 9500, "ratio": 3.36}]}',
     "tool_error": False},
    {"time": "t", "elapsed_s": 5.1, "event": "tool_call",
     "tool": "claimsight_tools_check_policy_period",
     "input": '{"policy_no": "POL-3004", "incident_date": "2026-05-30"}',
     "result": '{"within_period": true}', "tool_error": False},
    {"time": "t", "elapsed_s": 7.8, "event": "task_done", "agent": "Motor Claims Fraud Analyst",
     "task": "Investigate claim for fraud.", "output": "PRICES: windshield 3.4x market.\nRISK: high"},
    {"time": "t", "elapsed_s": 9.0, "event": "crew_done", "crew": "assessment",
     "seconds": 9.0, "total_tokens": 38412},
    {"time": "t", "elapsed_s": 9.2, "event": "run_finished", "total_seconds": 9.2,
     "tool_calls": 2, "tasks": 3, "errors": 0, "retried_llm_failures": 0},
]


def write(claim_state, log=LOG, queue=False):
    claim_id = claim_state["claim_id"]
    folder = reporting.REVIEW_QUEUE if queue else reporting.DECISIONS
    (folder / f"{claim_id}.json").write_text(json.dumps(claim_state, indent=2))
    if log is not None:
        (reporting.LOGS / f"{claim_id}.jsonl").write_text(
            "\n".join(json.dumps(dict(e, claim_id=claim_id)) for e in log) + "\n")
    return claim_id


def check(name, condition, detail=""):
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))
    return bool(condition)


def main() -> None:
    passed = total = 0

    def run(name, condition, detail=""):
        nonlocal passed, total
        total += 1
        passed += check(name, condition, detail)

    # ------------------------------------------------ a normal, covered claim
    print("A covered claim with one line denied\n")
    write(state("CLM-7001", "covered", ["Section I item 6: accidental external means"],
                "awaiting_review", "human_review"), queue=True)
    r = reporting.for_claim("CLM-7001")
    run("the report builds from the saved files", r is not None)
    run("coverage reason is marked as cited",
        r.coverage_reasons and r.coverage_reasons[0].grounded is True)
    tools = [e for e in r.evidence if e.kind == "tool"]
    run("both tool results are in the evidence trail, with timings",
        len(tools) == 2 and all(e.at_seconds for e in tools),
        ", ".join(f"{e.source.split(' (')[0]} at {e.at_seconds}s" for e in tools))
    run("the price-check result is quoted, not paraphrased",
        any('"ratio": 3.36' in e.detail for e in tools))
    run("the analyst's own report is kept",
        any(e.kind == "analysis" and "RISK: high" in e.detail for e in r.evidence))
    run("the invoice and policy schedule are listed as documents",
        len([e for e in r.evidence if e.kind == "document"]) >= 2)
    run("the route reason is the router's own words, without the route name",
        r.route_reason == "fraud score 0.70 >= 0.5", r.route_reason)
    run("timing and tokens come through from the log",
        r.run.get("total_seconds") == 9.2 and r.run.get("total_tokens") == 38412)
    text = reporting.explanation(r)
    run("the explanation names the denied line and its reason",
        "Windshield" in text and "above market rate" in text, text)
    run("the explanation does not promise a payment while a person is reviewing",
        "approved" not in text.lower())
    rendered = reporting.render(r)
    run("the rendered report shows the settlement as computed in code",
        "computed in code" in rendered and "16,081.97" in rendered)
    run("markdown saves too", reporting.save(r, markdown=True).exists())

    # ------------------------------------------------ coverage could not be settled
    print("\nA claim whose coverage the wording does not settle\n")
    write(state("CLM-7002", "uncertain", ["searched Section I; nothing settles a kerb impact"],
                "awaiting_review", "human_review",
                uncertainty=["the wording does not say whether a kerb impact is an accident"],
                doubts=["the Policy Analyst could not establish coverage"]))
    r = reporting.for_claim("CLM-7002")
    asks = r.outstanding_evidence
    run("it asks a person, not the claimant",
        asks and all(a.from_whom == "adjuster" for a in asks),
        "; ".join(f"{a.from_whom}: {a.item}" for a in asks))
    run("one coverage question, not one per phrasing of it", len(asks) == 1,
        f"{len(asks)} request(s): {[a.why for a in asks]}")
    run("the doubt is carried into the report", bool(r.coverage_doubts))
    text = reporting.explanation(r)
    run("the explanation says a claims officer will decide",
        "claims officer" in text, text)
    run("the explanation does not say the claim is covered or excluded",
        "does not cover" not in text and "policy covers" not in text)

    # ------------------------------------------------ an exclusion with no clause
    print("\nAn exclusion with no clause named\n")
    write(state("CLM-7003", "excluded", ["this looks like wear and tear to me"],
                "awaiting_review", "human_review",
                lines=[dict(LINES[0], admitted=False), dict(LINES[1], admitted=False,
                                                            reason="not covered")],
                doubts=["the exclusion names no policy clause"], payout=0.0))
    r = reporting.for_claim("CLM-7003")
    run("the reason is marked NOT cited", r.coverage_reasons[0].grounded is False)
    run("the rendered report flags it to the reader",
        "NO CLAUSE CITED" in reporting.render(r))

    # ------------------------------------------------ missing documents
    print("\nA claim missing documents\n")
    write(state("CLM-7004", "", [], "awaiting_evidence", "needs_evidence",
                missing=["invoice total", "incident date"], payout=0.0, assessed=False),
          log=LOG[:2])
    r = reporting.for_claim("CLM-7004")
    asks = [a for a in r.outstanding_evidence if a.from_whom == "claimant"]
    run("both missing documents are asked of the claimant", len(asks) == 2,
        "; ".join(a.item for a in asks))
    run("a claim stopped before assessment does not read as not covered",
        r.coverage_status == "", f"coverage_status={r.coverage_status!r}")
    run("the rendered report says it was not assessed",
        "NOT ASSESSED" in reporting.render(r))
    text = reporting.explanation(r)
    run("the explanation lists what the claimant must send",
        "invoice total" in text, text)
    run("the explanation makes no coverage claim",
        "does not cover" not in text and "policy covers" not in text, text)

    # ------------------------------------------------ a claim never assessed
    print("\nA claim that has not been assessed\n")
    run("no report, rather than an invented one", reporting.for_claim("CLM-9999") is None)

    # ------------------------------------------------ period reasons vs the tool
    print("\nA coverage reason about the cover dates, checked against the tool\n")
    period_log = [{"time": "t", "elapsed_s": 1.0, "event": "tool_call",
                   "tool": "claimsight_tools_check_policy_period",
                   "input": '{"policy_no": "POL-3004"}',
                   "result": '{"within_period": true}', "tool_error": False}]
    write(state("CLM-7007", "covered",
                ["Section I, item 6 - accidental external means",
                 "Period: within policy term 2026-01-07 to 2027-01-06"],
                "approved", "auto_approve", final=16081.97), log=period_log)
    r = reporting.for_claim("CLM-7007")
    marks = [reporting._check_mark(ref) for ref in r.coverage_reasons]
    run("a clause reason is marked as cited", marks[0] == "clause cited", marks[0])
    run("a period reason is verified against the tool, not treated as a missing clause",
        marks[1] == "tool verified", marks[1])

    write(state("CLM-7008", "excluded", ["the incident is outside the policy period"],
                "awaiting_review", "human_review",
                lines=[dict(LINES[0], admitted=False), dict(LINES[1], admitted=False,
                                                            reason="not covered")],
                payout=0.0), log=period_log)
    r = reporting.for_claim("CLM-7008")
    mark = reporting._check_mark(r.coverage_reasons[0])
    run("an agent that contradicts the tool is called out", mark == "DISAGREES WITH TOOL", mark)
    run("the rendered report shows the disagreement",
        "DISAGREES WITH TOOL" in reporting.render(r))

    write(state("CLM-7009", "covered", ["within the policy period"],
                "approved", "auto_approve"), log=[])
    r = reporting.for_claim("CLM-7009")
    mark = reporting._check_mark(r.coverage_reasons[0])
    run("with no tool result to compare, it says so rather than guessing",
        mark == "not checked", mark)

    # ------------------------------------------------ one tool call, one log entry
    print("\nA tool call reported by both the step callback and the guardrail hook\n")
    import monitoring
    monitoring.LOGS = reporting.LOGS
    monitor = monitoring.ClaimMonitor("CLM-7006")
    # the hook's wording, then the step callback's wording for the same call
    monitor.record_tool_call("claimsight_tools_check_policy_period",
                             {"policy_no": "POL-3004", "incident_date": "2026-05-30"},
                             '{"within_period": true}')
    monitor.record_tool_call("./claimsight_tools_check_policy_period",
                             {"policy_no": "POL-3004", "incident_date": "2026-05-30"},
                             '{"within_period": true, "guardrail_note": "checked"}')
    monitor.record_tool_call("claimsight_tools_check_part_prices",
                             {"line_items": []}, '{"error": "line_items is empty"}')
    entries = [e for e in reporting.load_log("CLM-7006") if e["event"] == "tool_call"]
    run("the same call reported twice is logged once", len(entries) == 2,
        "; ".join(e["tool"] for e in entries))
    run("the raw tool result is kept, not the annotated one",
        "guardrail_note" not in entries[0]["result"])
    run("the tool count matches", monitor.counts["tool_calls"] == 2,
        f"tool_calls={monitor.counts['tool_calls']}")
    run("a tool that returned an error is marked as one",
        entries[1]["tool_error"] is True)

    # ------------------------------------------------ PII never leaves over MCP
    print("\nPersonal data in the file, fetched over MCP\n")
    write(state("CLM-7005", "covered", ["Section I item 6"], "approved", "auto_approve",
                narrative=NARRATIVE_WITH_PII, final=16081.97))
    import mcp_server
    mcp_server.reporting = reporting            # same temporary folders
    payload = mcp_server.get_claim_report("CLM-7005")
    blob = json.dumps(payload)
    run("the narrative is in the local report",
        NARRATIVE_WITH_PII[:20] in json.dumps(reporting.for_claim("CLM-7005").model_dump()))
    run("the phone number is not in what MCP returns", "9876543210" not in blob)
    run("the email address is not in what MCP returns", "ravi.kumar@example.com" not in blob)
    run("it says how much it redacted", payload.get("pii_redacted", 0) >= 2,
        f"pii_redacted={payload.get('pii_redacted')}")
    run("a malformed claim id is refused",
        "error" in mcp_server.get_claim_report("DROP TABLE"))
    run("an unassessed claim is refused",
        "error" in mcp_server.get_claim_report("CLM-9999"))

    print(f"\n{passed}/{total} report checks as expected")
    print(f"(temporary files in {TMP})")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
