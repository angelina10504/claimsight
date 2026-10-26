"""
Everything the dashboard reads, and nothing it should not.

The dashboard is a window onto what ClaimSight has already recorded. It never
decides anything: coverage, fraud scores, settlements and routes all come from
the saved Flow state, and the explanation from src/reporting.py, the same code
show_report.py uses. Nothing here imports CrewAI, ADK or a model client, so
opening the dashboard cannot spend a token.

What is kept out of the page:
    the dataset's labels      ground_truth, fault_type, the per-line
                              "admitted" answers and any evaluation score
    personal fields           the policyholder's name, phone and address
    the filesystem            only fixed folders are read, by validated id,
                              and no path is ever shown
"""

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import reporting                                   # noqa: E402

DATA = ROOT / "data"
CLAIM_RE = re.compile(r"^CLM-\d{4}$")
POLICY_RE = re.compile(r"^POL-\d{4}$")
DEMO_IDS = ("CLM-9001", "CLM-9002")
PII_FIELDS = ("insured_name", "phone", "address")   # as crews/intake_crew.py drops them
SUBMISSION_FIELDS = ("claim_id", "policy_no", "incident_date", "invoice_date", "narrative")


def _paths() -> dict:
    """Read at call time, so a test can point DATA somewhere else."""
    return {"claims": DATA / "claims", "policies": DATA / "policies",
            "intake": DATA / "intake", "assessment": DATA / "assessment",
            "invoices": DATA / "invoices", "manifest": DATA / "manifest.json"}


def valid_claim_id(claim_id) -> bool:
    return isinstance(claim_id, str) and bool(CLAIM_RE.match(claim_id))


def _read_json(path: Path):
    """(data, problem). A missing file is (None, None); an unreadable one says
    so without saying where it lives."""
    if not path.exists():
        return None, None
    try:
        return json.loads(path.read_text()), None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None, "the saved file could not be read"


# ---------------------------------------------------------------- the dataset
def manifest() -> List[dict]:
    """The official 60, from data/manifest.json, without fault_type: that is a
    label, and the dashboard shows no labels."""
    data, _ = _read_json(_paths()["manifest"])
    return [{"claim_id": e["claim_id"], "policy_no": e.get("policy_no"),
             "split": e.get("split")} for e in (data or []) if valid_claim_id(e.get("claim_id"))]


def dataset_counts() -> dict:
    entries = manifest()
    return {"total": len(entries),
            "dev": sum(e["split"] == "dev" for e in entries),
            "held_out": sum(e["split"] == "test" for e in entries)}


def known_claims() -> List[str]:
    """Every claim the dashboard may open: the official 60, plus a demo fixture
    if one has been built. Nothing else, whatever is in the folder."""
    ids = [e["claim_id"] for e in manifest()]
    ids += [d for d in DEMO_IDS if (_paths()["claims"] / f"{d}.json").exists()]
    return ids


def is_known(claim_id) -> bool:
    return valid_claim_id(claim_id) and claim_id in known_claims()


def split_of(claim_id: str) -> str:
    for entry in manifest():
        if entry["claim_id"] == claim_id:
            return {"dev": "development", "test": "held out"}.get(entry["split"], entry["split"])
    return "demonstration fixture" if claim_id in DEMO_IDS else "unknown"


def claim_submission(claim_id: str) -> Optional[dict]:
    """What the claimant submitted, as the Intake crew's read_claim_submission
    tool sees it: no labels, no line-by-line answers."""
    if not is_known(claim_id):
        return None
    data, _ = _read_json(_paths()["claims"] / f"{claim_id}.json")
    if not data:
        return None
    out = {k: data.get(k) for k in SUBMISSION_FIELDS}
    out["photos"] = len(data.get("photos") or [])
    return out


def policy_details(policy_no) -> Optional[dict]:
    if not (isinstance(policy_no, str) and POLICY_RE.match(policy_no)):
        return None
    data, _ = _read_json(_paths()["policies"] / f"{policy_no}.json")
    if not data:
        return None
    return {k: v for k, v in data.items() if k not in PII_FIELDS}


def invoice_pdf(claim_id: str) -> Optional[bytes]:
    if not is_known(claim_id):
        return None
    path = _paths()["invoices"] / f"{claim_id}.pdf"
    return path.read_bytes() if path.exists() else None


def has_saved_intake(claim_id: str) -> bool:
    return is_known(claim_id) and (_paths()["intake"] / f"{claim_id}.json").exists()


# ---------------------------------------------------------------- statuses
# One vocabulary for every page. key -> (label, tone, icon)
STATUS = {
    "approved": ("Approved", "good", "✓"),
    "approved_by_adjuster": ("Approved by adjuster", "good", "✓"),
    "rejected": ("Rejected", "bad", "✕"),
    "rejected_by_adjuster": ("Rejected by adjuster", "bad", "✕"),
    "awaiting_review": ("Awaiting human review", "warn", "◐"),
    "awaiting_evidence": ("Awaiting evidence", "warn", "◌"),
    "failed": ("Failed", "bad", "!"),
    "assessed_only": ("Assessed outside the Flow", "info", "○"),
    "not_assessed": ("Not yet assessed", "neutral", "○"),
    "unavailable": ("Unavailable", "neutral", "?"),
}

ROUTE = {"auto_approve": "Automatic approval", "human_review": "Human review",
         "reject": "Rejection", "needs_evidence": "Evidence request"}


def status_label(key: str) -> tuple:
    return STATUS.get(key, (key.replace("_", " ").capitalize() if key else "Unknown",
                            "neutral", "○"))


# ---------------------------------------------------------------- one claim
@dataclass
class ClaimRecord:
    claim_id: str
    source: str                         # flow | assessment_only | none | unreadable
    status: str                         # a STATUS key
    state: Optional[dict] = None
    report: object = None               # reporting.ClaimReport, for a Flow record
    log: List[dict] = field(default_factory=list)
    saved_at: Optional[str] = None
    problem: Optional[str] = None
    legacy: Optional[dict] = None       # decision + settlement from run_assessment.py
    last_run_failed: bool = False


def _saved_at(claim_id: str) -> Optional[str]:
    times = [p.stat().st_mtime for p in (reporting.DECISIONS / f"{claim_id}.json",
                                         reporting.REVIEW_QUEUE / f"{claim_id}.json")
             if p.exists()]
    if not times:
        return None
    return datetime.fromtimestamp(max(times)).strftime("%d %b %Y, %H:%M")


def load_record(claim_id: str) -> ClaimRecord:
    """The saved result for a claim, in whichever form exists."""
    if not is_known(claim_id):
        return ClaimRecord(claim_id=str(claim_id)[:20], source="none", status="unavailable",
                           problem="not a claim this dashboard can open")
    log = reporting.load_log(claim_id)
    finished = next((e for e in reversed(log) if e.get("event") == "run_finished"), {})
    try:
        state = reporting.load_state(claim_id)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return ClaimRecord(claim_id=claim_id, source="unreadable", status="unavailable",
                           log=log, problem="the saved decision could not be read")

    if state:
        report = reporting.build_report(state, log)
        return ClaimRecord(claim_id=claim_id, source="flow",
                           status=state.get("status") or "not_assessed",
                           state=state, report=report, log=log, saved_at=_saved_at(claim_id),
                           last_run_failed=finished.get("status") == "failed")

    legacy, problem = _read_json(_paths()["assessment"] / f"{claim_id}.json")
    if legacy and legacy.get("decision"):
        # run_assessment.py's output: a decision and a settlement, but no route,
        # because it never went through the Flow. Its "score" compares the
        # result with the labels, so it is not carried over.
        when = datetime.fromtimestamp(
            (_paths()["assessment"] / f"{claim_id}.json").stat().st_mtime)
        return ClaimRecord(claim_id=claim_id, source="assessment_only", status="assessed_only",
                           saved_at=when.strftime("%d %b %Y, %H:%M"), log=log, legacy={"decision": legacy.get("decision") or {},
                                            "settlement": legacy.get("settlement") or {}})
    if finished.get("status") == "failed":
        return ClaimRecord(claim_id=claim_id, source="none", status="failed", log=log,
                           problem=f"the last run failed at {finished.get('failed_at', 'a step')}")
    return ClaimRecord(claim_id=claim_id, source="unreadable" if problem else "none",
                       status="unavailable" if problem else "not_assessed",
                       log=log, problem=problem)


def saved_claims() -> List[ClaimRecord]:
    """Every claim with something saved, newest first."""
    out = []
    for claim_id in known_claims():
        record = load_record(claim_id)
        if record.source in ("flow", "assessment_only") or record.status == "failed":
            out.append(record)
    out.sort(key=lambda r: (r.source != "flow", -_mtime(r.claim_id)))
    return out


def _mtime(claim_id: str) -> float:
    paths = [reporting.DECISIONS / f"{claim_id}.json", reporting.REVIEW_QUEUE / f"{claim_id}.json",
             _paths()["assessment"] / f"{claim_id}.json"]
    return max([p.stat().st_mtime for p in paths if p.exists()] or [0.0])


def overview_counts() -> dict:
    records = saved_claims()
    flow = [r for r in records if r.source == "flow"]
    return {"saved": len(records),
            "through_flow": len(flow),
            "approved": sum(r.status in reporting.AUTHORISED for r in flow),
            "rejected": sum(r.status in reporting.REFUSED for r in flow),
            "awaiting_review": sum(r.status == "awaiting_review" for r in flow),
            "awaiting_evidence": sum(r.status == "awaiting_evidence" for r in flow),
            "assessed_only": sum(r.source == "assessment_only" for r in records)}


# ---------------------------------------------------------------- the money
def money(record: ClaimRecord) -> dict:
    """Proposed and authorised amounts, as recorded. A claim waiting on a
    person or on documents has NO final figure: authorised is None, never 0."""
    if record.source == "flow":
        report = record.report
        settlement = record.state.get("settlement") or {}
        state = report.settlement_state
        return {"state": state, "label": reporting.SETTLEMENT_STATE[state],
                "proposed": settlement.get("payout") if settlement else None,
                "authorised": report.authorised_payout,
                "breakdown": settlement if "admitted_subtotal" in settlement else None,
                "explanation": settlement.get("explanation", "")}
    if record.source == "assessment_only":
        settlement = record.legacy["settlement"]
        return {"state": "not_assessed", "label": "Assessed outside the Flow: never routed, "
                "so nothing was authorised", "proposed": settlement.get("payout"),
                "authorised": None,
                "breakdown": settlement if "admitted_subtotal" in settlement else None,
                "explanation": settlement.get("explanation", "")}
    return {"state": "not_assessed", "label": "Not assessed", "proposed": None,
            "authorised": None, "breakdown": None, "explanation": ""}


def final_payment_text(record: ClaimRecord) -> str:
    m = money(record)
    if m["authorised"] is not None:
        return ("Refused: nothing payable" if m["state"] == "refused"
                else f"₹{m['authorised']:,.2f} authorised")
    return {"pending_human_review": "Pending human review",
            "pending_evidence": "Pending: waiting for evidence"}.get(m["state"], "Not authorised")


# ---------------------------------------------------------------- invoice lines
LINE_STATUS = {
    "PROPOSE PAY": ("Admitted (proposed)", "good"),
    "DISPUTED": ("Disputed, awaiting a decision", "warn"),
    "PAID": ("Paid", "good"),
    "NOT PAID": ("Not paid", "bad"),
    "DENIED": ("Denied by adjuster", "bad"),
}


def reference_prices(log: List[dict]) -> dict:
    """Market prices as check_part_prices answered them, by line description.
    Taken from the recorded tool call, not looked up again."""
    out = {}
    for entry in log:
        if entry.get("event") != "tool_call" or "check_part_prices" not in str(entry.get("tool")):
            continue
        text = str(entry.get("result") or "")
        try:
            answer = json.loads(text)
            lines = (answer.get("lines") or []) if isinstance(answer, dict) else []
        except (json.JSONDecodeError, TypeError):
            # The log keeps the first 600 characters of a long result, so the
            # whole answer no longer parses; the line objects that were kept
            # in full still do. A line after the cut is simply not recorded.
            lines = []
            for chunk in re.findall(r"\{[^{}]*\}", text):
                try:
                    lines.append(json.loads(chunk))
                except json.JSONDecodeError:
                    continue
        for line in lines:
            if isinstance(line, dict) and line.get("market_price") is not None:
                out[line.get("description")] = {"market_price": line["market_price"],
                                                "ratio": line.get("ratio"),
                                                "check": line.get("check")}
    return out


def line_rows(record: ClaimRecord) -> List[dict]:
    if record.source == "flow":
        lines, state = record.state.get("decision", {}).get("line_decisions") or [], \
            record.report.settlement_state
        by_person = record.status in reporting.BY_A_PERSON
    elif record.source == "assessment_only":
        lines, state, by_person = record.legacy["decision"].get("line_decisions") or [], \
            "not_assessed", False
    else:
        return []
    prices = reference_prices(record.log)
    rows = []
    for line in lines:
        label = reporting.line_label(bool(line.get("admitted")), state, by_person).strip()
        text, tone = LINE_STATUS.get(label, (label.title(), "neutral"))
        rows.append({"description": line.get("description", ""),
                     "amount": float(line.get("amount") or 0.0),
                     "status": text, "tone": tone, "reason": line.get("reason") or "",
                     "reference": prices.get(line.get("description"))})
    return rows


# ---------------------------------------------------------------- the run
def run_facts(record: ClaimRecord) -> dict:
    """What the log measured. Anything not recorded is None, never guessed."""
    log = record.log
    started = next((e for e in log if e.get("event") == "run_started"), {})
    finished = next((e for e in reversed(log) if e.get("event") == "run_finished"), {})
    crews = {e.get("crew"): e for e in log if e.get("event") == "crew_done"}
    tokens = [c.get("total_tokens") for c in crews.values() if c.get("total_tokens")]
    return {
        "recorded": bool(log),
        "model": started.get("model"), "provider": started.get("provider"),
        "total_seconds": finished.get("total_seconds"),
        "tool_calls": finished.get("tool_calls",
                                   sum(e.get("event") == "tool_call" for e in log) if log else None),
        "tasks": finished.get("tasks"),
        "retried_llm_failures": finished.get("retried_llm_failures"),
        "llm_failures": [e.get("kind") for e in log if e.get("event") == "llm_call_failed"],
        "errors": finished.get("errors"),
        "total_tokens": sum(tokens) if tokens else None,
        "crews": {name: {"seconds": c.get("seconds"), "total_tokens": c.get("total_tokens"),
                         "prompt_tokens": c.get("prompt_tokens"),
                         "completion_tokens": c.get("completion_tokens"),
                         "reasoning_tokens": c.get("reasoning_tokens")}
                  for name, c in crews.items()},
        "trace_id": finished.get("trace_id") or started.get("trace_id"),
        "final_status": finished.get("status"),
        "started_at": (started.get("time") or "")[:19].replace("T", " ") or None,
    }


ANALYSTS = ("Policy Analyst", "Fraud Analyst")


def task_spans(log: List[dict]) -> List[dict]:
    """Start and end of each agent's task, from the recorded timestamps.

    A task's end is when it was logged done. Its start is the end of the task
    before it, except for the two analysts, which the Flow starts together: they
    begin when the "running in parallel" entry was written."""
    parallel_from = next((e.get("elapsed_s") for e in log if e.get("event") == "flow"
                          and "parallel" in str(e.get("message", ""))), None)
    spans = []
    for entry in log:
        if entry.get("event") != "task_done" or entry.get("elapsed_s") is None:
            continue
        end = float(entry["elapsed_s"])
        agent = str(entry.get("agent") or "agent")
        if parallel_from is not None and any(a in agent for a in ANALYSTS):
            start = float(parallel_from)
        else:
            start = max(0.0, end - float(entry.get("seconds_since_previous_task") or 0.0))
        spans.append({"agent": agent, "start": round(start, 2), "end": round(end, 2),
                      "seconds": round(end - start, 2),
                      "parallel": any(a in agent for a in ANALYSTS)})
    return spans


def tool_name(raw) -> str:
    return reporting._tool_name(str(raw or ""))


def timeline(log: List[dict]) -> List[dict]:
    """The log as steps a person can follow. Each step keeps the raw entry for
    the expandable detail; the page decides what to show."""
    steps = []
    for e in log:
        kind, at = e.get("event"), e.get("elapsed_s")
        step = None
        if kind == "run_started":
            model = e.get("model") or "Not recorded"
            step = ("start", "Claim processing started", f"Model: {model}", "info")
        elif kind == "flow":
            message = str(e.get("message", ""))
            if e.get("router") == "route_claim" or message.startswith("router 2"):
                step = ("route", "Routing decision (Python rules)",
                        message.split(": ", 1)[-1], "info")
            elif message.startswith("router 1"):
                step = ("route", "Completeness check (Python rules)",
                        message.split(": ", 1)[-1], "info")
            elif message.startswith("assessment: coverage="):
                step = ("settle", "Settlement calculated (Python)",
                        message.split("assessment: ", 1)[-1], "good")
            elif "parallel" in message:
                step = ("crew", "Policy Analyst and Fraud Analyst start in parallel",
                        "Assessment crew", "info")
            elif message.startswith("intake:"):
                step = ("crew", "Intake", message.split("intake: ", 1)[-1], "info")
            elif message.startswith(("auto-approved", "rejected", "paused", "adjuster")):
                step = ("final", "Outcome", message, "good")
            elif message.startswith("evidence request"):
                step = ("final", "Evidence requested", message.split(": ", 1)[-1], "warn")
            else:
                step = ("flow", "Flow", message, "neutral")
        elif kind == "task_done":
            secs = e.get("seconds_since_previous_task")
            step = ("agent", f"{e.get('agent')} finished",
                    (f"{secs}s since the previous task" if secs is not None else "")
                    + (f" · returned {e['structured']}" if e.get("structured") else ""), "good")
        elif kind == "tool_call":
            name = tool_name(e.get("tool"))
            meaning = reporting.TOOL_MEANING.get(name, "")
            step = ("tool", f"MCP tool call: {name}",
                    (meaning + (" · returned an error" if e.get("tool_error") else "")),
                    "bad" if e.get("tool_error") else "info")
        elif kind == "guardrail":
            passed = e.get("passed", True)
            what = {"input_screening": "Input screening", "document_sanitised": "Document cleaned",
                    "tool_result": "Tool result check", "report_format": "Report format check",
                    "decision": "Decision check", "pii_redacted": "PII redacted"
                    }.get(e.get("kind"), str(e.get("kind")))
            step = ("guard", f"Guardrail: {what}", "passed" if passed else "stopped the output",
                    "good" if passed else "warn")
        elif kind == "crew_done":
            tokens = e.get("total_tokens")
            step = ("crew", f"{str(e.get('crew')).capitalize()} crew finished",
                    f"{e.get('seconds')}s" + (f" · {tokens:,} tokens" if tokens else
                                              " · tokens not recorded"), "good")
        elif kind == "llm_call_failed":
            step = ("fail", "Model call failed and was retried",
                    str(e.get("kind", "")).replace("_", " "), "warn")
        elif kind in ("error", "tool_error"):
            step = ("fail", "Error", str(e.get("where") or e.get("tool") or ""), "bad")
        elif kind == "run_finished":
            label, tone, _ = status_label(e.get("status") or "")
            step = ("end", "Final state", f"{label} · {e.get('total_seconds')}s in total",
                    {"good": "good", "bad": "bad"}.get(tone, "warn"))
        elif kind == "agent_answer":
            continue                        # shown with its task, in the detail
        if step:
            steps.append({"kind": step[0], "title": step[1], "detail": step[2],
                          "tone": step[3], "at": at, "entry": e})
    return steps


def sanitise(text) -> str:
    """Tool input and output, with phone numbers, emails and ID numbers masked."""
    import guardrails
    cleaned, _ = guardrails.redact_pii(str(text or ""))
    return cleaned


def explanation(record: ClaimRecord) -> str:
    return reporting.explanation(record.report) if record.source == "flow" else ""


def report_downloads(record: ClaimRecord) -> dict:
    """The existing report, as Markdown and JSON, built in memory. Nothing is
    written to data/reports."""
    if record.source != "flow":
        return {}
    return {"markdown": reporting.to_markdown(record.report),
            "json": json.dumps(record.report.model_dump(), indent=2, default=str)}
