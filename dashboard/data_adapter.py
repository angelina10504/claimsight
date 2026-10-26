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
sys.path.append(str(ROOT / "src" / "tools"))       # photo_check, which needs no CrewAI

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
            "invoices": DATA / "invoices", "manifest": DATA / "manifest.json",
            "photo_manifest": DATA / "photo_manifest.json", "photos": DATA / "photos"}


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


def photo_manifest() -> List[dict]:
    """The photo claims, from data/photo_manifest.json, without fault_type."""
    data, _ = _read_json(_paths()["photo_manifest"])
    return [{"claim_id": e["claim_id"], "policy_no": e.get("policy_no"), "split": "photo"}
            for e in (data or []) if valid_claim_id(e.get("claim_id"))]


def known_claims() -> List[str]:
    """Every claim the dashboard may open: the development claims in
    data/manifest.json and the photo claims in data/photo_manifest.json.
    The 40 held-out claims are not selectable: they are for the final
    evaluation, not for browsing. Nothing else, whatever is in the folder."""
    ids = [e["claim_id"] for e in manifest() if e["split"] == "dev"]
    ids += [e["claim_id"] for e in photo_manifest() if e["claim_id"] not in ids]
    return ids


def is_known(claim_id) -> bool:
    return valid_claim_id(claim_id) and claim_id in known_claims()


def split_of(claim_id: str) -> str:
    for entry in manifest():
        if entry["claim_id"] == claim_id:
            return {"dev": "development", "test": "held out"}.get(entry["split"], entry["split"])
    if any(e["claim_id"] == claim_id for e in photo_manifest()):
        return "photo"
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


# ================================================================ the walkthrough
# Everything below feeds the Claim Walkthrough page. It reads the saved Flow
# state, the saved intake and the claim's monitoring log; nothing is re-run.

import vision                                      # noqa: E402  (PARTS only; no model is loaded)
from photo_check import check_billed_parts_against_photo   # noqa: E402

PHOTO_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}\.(jpe?g|png)$", re.IGNORECASE)


# ---------------------------------------------------------------- the photograph
def claim_photos(claim_id: str) -> List[dict]:
    """The claim's photographs by name, and whether each is on file. Names
    are checked, and photos are read only from data/photos."""
    if not is_known(claim_id):
        return []
    data, _ = _read_json(_paths()["claims"] / f"{claim_id}.json")
    out = []
    for name in (data or {}).get("photos") or []:
        if isinstance(name, str) and PHOTO_RE.match(name):
            path = _paths()["photos"] / name
            out.append({"name": name, "available": path.is_file() and path.stat().st_size > 0})
    return out


def photo_bytes(claim_id: str, name: str) -> Optional[bytes]:
    """One photograph, only if it belongs to this claim."""
    if not any(p["name"] == name and p["available"] for p in claim_photos(claim_id)):
        return None
    return (_paths()["photos"] / name).read_bytes()


def _intake_of(record: ClaimRecord) -> dict:
    """The intake record: from the Flow state, else the saved Intake output."""
    intake = (record.state or {}).get("intake") or {}
    if intake:
        return intake
    if is_known(record.claim_id):
        data, _ = _read_json(_paths()["intake"] / f"{record.claim_id}.json")
        return data or {}
    return {}


def damage_findings(record: ClaimRecord) -> dict:
    """What the Damage Analyst recorded for each photograph, and where from.

    The intake record's photo_damage is the agent's checked output; failing
    that, the vision inspections in the log. Neither exists until the claim
    has been through the Flow with its photographs."""
    tokens = {}
    model = None
    from_log = []
    for e in record.log:
        if e.get("event") == "photo_inspected":
            model = e.get("model") or model
            used = [e.get("prompt_tokens"), e.get("completion_tokens")]
            if any(isinstance(t, (int, float)) for t in used):
                tokens[e.get("photo")] = sum(t for t in used if isinstance(t, (int, float)))
            if "damaged" in e:
                from_log.append({"photo": e.get("photo"), "damaged": e.get("damaged") or [],
                                 "undamaged": e.get("undamaged") or [],
                                 "not_visible": e.get("not_visible") or [],
                                 "damage_types": e.get("damage_types") or [],
                                 "readable": bool(e.get("readable")), "severity": None,
                                 "notes": ""})
    recorded = _intake_of(record).get("photo_damage") or []
    photos = recorded or from_log
    return {"photos": [{**p, "notes": sanitise(p.get("notes"))} for p in photos],
            "source": ("the intake record" if recorded else
                       "the vision log" if from_log else None),
            "model": model, "tokens": tokens}


def photo_comparison(record: ClaimRecord, finding: dict) -> Optional[dict]:
    """The billed parts against the photograph.

    The Flow's own check, as saved in the claim state, when there is one:
    that is what decided the route ("recorded": True). For a run saved before
    the Flow made the check, the same code (check_billed_parts_against_photo)
    is run here on the saved findings, and marked as not having acted on
    anything. None when there is no extracted invoice to compare with."""
    recorded = (record.state or {}).get("photo_check")
    if recorded:
        return {**recorded, "recorded": True}
    lines = ((_intake_of(record).get("invoice") or {}).get("line_items")) or []
    if not lines:
        return None
    return {**check_billed_parts_against_photo(
        [{"description": l.get("description", ""), "amount": l.get("amount")} for l in lines],
        finding.get("damaged") or [], finding.get("undamaged") or [],
        finding.get("not_visible") or [], photo_readable=bool(finding.get("readable"))),
        "recorded": False}


# ---------------------------------------------------------------- the six agents
# In the order they run. "role" is the exact role string in the crew code,
# which is what the log records against each task.
AGENTS = [
    {"key": "document_reader", "name": "Document Reader", "crew": "intake",
     "role": "Motor Claims Document Reader", "framework": "CrewAI",
     "does": "Reads the repair invoice into structured line items.", "tools": []},
    {"key": "intake_officer", "name": "Claims Intake Officer", "crew": "intake",
     "role": "Claims Intake Officer", "framework": "CrewAI",
     "does": "Assembles the claim record and says whether the file is complete. "
             "Derives the damaged parts from the invoice.", "tools": []},
    {"key": "damage_analyst", "name": "Damage Analyst", "crew": "intake",
     "role": "Damage Analyst", "framework": "CrewAI + vision model",
     "does": "Records what each photograph shows, part by part. Never sees the invoice.",
     "tools": []},
    {"key": "policy_analyst", "name": "Policy Analyst", "crew": "assessment",
     "role": "Motor Insurance Policy Analyst", "framework": "CrewAI + RAG",
     "does": "Decides coverage from the policy wording, citing a clause for each finding. "
             "Runs in parallel with the Fraud Analyst.", "tools": ["check_policy_period"]},
    {"key": "fraud_analyst", "name": "Fraud Analyst", "crew": "assessment",
     "role": "Motor Claims Fraud Analyst", "framework": "CrewAI",
     "does": "Looks for inflated, repeated or inconsistent claims, using the tools for "
             "every fact. Runs in parallel with the Policy Analyst.",
     "tools": ["check_part_prices", "find_prior_claims"]},
    {"key": "assessment_lead", "name": "Assessment Lead", "crew": "assessment",
     "role": "Claims Assessment Lead", "framework": "CrewAI",
     "does": "Turns both reports into one line-by-line decision. Does not calculate the "
             "payout: Python does.", "tools": []},
]

# Guardrail checks that name the report or decision they checked.
_GUARD_OWNER = {("report_format", "coverage report"): "policy_analyst",
                ("report_format", "fraud report"): "fraud_analyst",
                ("decision", None): "assessment_lead"}
GUARD_LABEL = {"input_screening": "Input screening", "document_sanitised": "Document cleaned",
               "tool_result": "Tool result check", "report_format": "Report format check",
               "decision": "Decision check", "pii_redacted": "PII redacted"}


def _messages(log: List[dict]) -> List[str]:
    return [str(e.get("message", "")) for e in log if e.get("event") == "flow"]


def _owner_of_tool(tool: str) -> Optional[str]:
    name = tool_name(tool)
    return next((a["key"] for a in AGENTS if name in a["tools"]), None)


def agent_walkthrough(record: ClaimRecord) -> List[dict]:
    """Each agent: did it run in the recorded run, and if so what it was asked,
    what it concluded, which tools it called and what they answered, and which
    guardrails checked its work. Only what the log recorded; a gap says why."""
    log = record.log
    messages = _messages(log)
    done = {str(e.get("agent")): e for e in log if e.get("event") == "task_done"}
    spans = {s["agent"]: s for s in task_spans(log)}
    crews = {e.get("crew"): e for e in log if e.get("event") == "crew_done"}
    reused = any(m.startswith("intake: reused") for m in messages)
    stopped_at_gate = any(m.startswith("router 1: file incomplete") for m in messages)
    finished = next((e for e in reversed(log) if e.get("event") == "run_finished"), {})
    has_photos = bool(claim_photos(record.claim_id))

    out = []
    for agent in AGENTS:
        entry = done.get(agent["role"])
        item = {**agent, "ran": bool(entry), "reason": "", "asked": "", "concluded": "",
                "structured": None, "seconds": None, "started": None, "tools_called": [],
                "guardrails": [], "crew_tokens": None, "vision_tokens": None, "citations": []}
        crew = crews.get(agent["crew"]) or {}
        if crew.get("total_tokens"):
            item["crew_tokens"] = crew["total_tokens"]

        if entry:
            span = spans.get(agent["role"]) or {}
            item.update(asked=sanitise(entry.get("task")), concluded=sanitise(entry.get("output")),
                        structured=entry.get("structured") if entry.get("structured") not in
                        (None, "None") else None,
                        seconds=span.get("seconds"), started=span.get("start"))
        elif not log:
            item["reason"] = "No monitoring log is saved for this claim."
        elif agent["key"] == "damage_analyst" and not has_photos:
            item["reason"] = "No photograph on this claim, so the Damage Analyst is skipped."
        elif agent["crew"] == "intake" and reused:
            item["reason"] = ("Not run in this recorded run: the intake saved by an earlier "
                              "run was reused.")
        elif agent["crew"] == "assessment" and stopped_at_gate:
            item["reason"] = ("The claim stopped at the completeness check to wait for "
                              "evidence, so the assessment did not run.")
        elif finished.get("status") == "failed" or any(e.get("event") == "error" for e in log):
            item["reason"] = "The run ended before this agent finished a task."
        elif agent["key"] == "damage_analyst":
            item["reason"] = ("No Damage Analyst task is in this log: it was written before "
                              "the agent was added, or before the photo was inspected.")
        else:
            item["reason"] = "No task was recorded for this agent in this log."

        if agent["key"] == "damage_analyst":
            findings = damage_findings(record)
            if findings["tokens"]:
                item["vision_tokens"] = sum(findings["tokens"].values())

        for e in log:
            if e.get("event") == "tool_call":
                owner = next((a["key"] for a in AGENTS if a["role"] == e.get("agent")), None) \
                    or _owner_of_tool(e.get("tool"))
                if owner == agent["key"]:
                    item["tools_called"].append({
                        "tool": tool_name(e.get("tool")), "at": e.get("elapsed_s"),
                        "input": sanitise(e.get("input")), "result": sanitise(e.get("result")),
                        "error": str(e.get("tool_error")).lower() == "true"})
            elif e.get("event") == "guardrail":
                kind = e.get("kind")
                owner = _GUARD_OWNER.get((kind, e.get("report"))) or \
                    _GUARD_OWNER.get((kind, None))
                if kind == "tool_result":
                    owner = _owner_of_tool(e.get("tool"))
                if owner == agent["key"]:
                    item["guardrails"].append({
                        "what": GUARD_LABEL.get(kind, str(kind)), "at": e.get("elapsed_s"),
                        "passed": str(e.get("passed", True)).lower() != "false",
                        "tool": tool_name(e.get("tool")) if e.get("tool") else ""})

        if agent["key"] == "assessment_lead" and record.source == "flow" and item["ran"]:
            item["citations"] = [{"clause": r.detail, "source": r.source, "verified": r.grounded}
                                 for r in record.report.coverage_reasons]
        out.append(item)
    return out


def flow_checks(record: ClaimRecord) -> List[dict]:
    """Guardrail checks the Flow made that belong to no single agent."""
    owned = {"report_format", "decision", "tool_result"}
    return [{"what": GUARD_LABEL.get(e.get("kind"), str(e.get("kind"))), "at": e.get("elapsed_s"),
             "passed": str(e.get("passed", True)).lower() != "false"}
            for e in record.log if e.get("event") == "guardrail" and e.get("kind") not in owned]


# ---------------------------------------------------------------- the flow stages
STAGES = (("intake", "Intake"), ("gate", "Completeness gate"),
          ("assessment", "Parallel assessment"), ("settlement", "Settlement"),
          ("router", "Router"), ("outcome", "Outcome"))
STAGE_STATE = {"done": ("Done", "good"), "now": ("Current", "warn"), "fail": ("Failed", "bad"),
               "todo": ("Not reached", "neutral"), "skipped": ("Skipped", "neutral")}


def flow_stages(log: List[dict], state: Optional[dict] = None) -> dict:
    """The six stages of the Flow, each with what the log says it produced.

    The current stage is the one the claim is at: the first unfinished stage
    while a run is going, the outcome once it has finished, or the evidence
    request while the claim waits for a document."""
    state = state or {}
    messages = _messages(log)

    def first(prefix):
        return next((m for m in messages if m.startswith(prefix)), None)

    def after(text, sep=": "):
        return text.split(sep, 1)[-1] if text else ""

    finished = next((e for e in reversed(log) if e.get("event") == "run_finished"), None)
    errors = [e for e in log if e.get("event") == "error"]
    intake_line = next((m for m in messages if m.startswith("intake: ") and "invoice lines" in m), None)
    reused = first("intake: reused")
    gate = first("router 1:")
    incomplete = bool(gate and "incomplete" in gate)
    parallel = first("assessment: Policy Analyst")
    assessed = any(e.get("event") == "crew_done" and e.get("crew") == "assessment" for e in log)
    settled = first("assessment: coverage=")
    routed = first("router 2:")
    evidence = [m.split(": ", 1)[-1] for m in messages if m.startswith("evidence request")]

    stages = {
        "intake": ("done", (after(reused) if reused else "Intake crew ran")
                   + (f" · {after(intake_line)}" if intake_line else "")) if intake_line
        else ("now", "Intake crew running") if log else ("todo", ""),
        "gate": ("done", after(gate)) if gate else ("todo", ""),
        "assessment": ("done", "Policy and Fraud Analysts in parallel, then the Lead") if assessed
        else ("now", "Policy and Fraud Analysts running in parallel") if parallel
        else ("todo", ""),
        "settlement": ("done", after(settled, "assessment: ")) if settled else ("todo", ""),
        "router": ("done", after(routed)) if routed else ("todo", ""),
        "outcome": ("done", status_label((finished or {}).get("status") or "")[0])
        if finished else ("todo", ""),
    }
    if incomplete:
        for key in ("assessment", "settlement", "router"):
            stages[key] = ("skipped", "the claim is waiting for evidence")

    if errors or (finished and finished.get("status") == "failed"):
        where = str((errors[-1] if errors else {}).get("where") or "")
        target = next((k for k, _ in STAGES if stages[k][0] in ("now", "todo")), "outcome")
        if where in ("intake", "assessment") and stages[where][0] != "done":
            target = where
        stages[target] = ("fail", f"stopped here: {str((errors[-1] if errors else {}).get('error_type') or 'error')}")

    if not finished:
        current = next((k for k, _ in STAGES if stages[k][0] in ("now", "fail")), None)
    else:
        current = next((k for k, _ in STAGES if stages[k][0] == "fail"), "outcome")
    waiting = incomplete or state.get("status") == "awaiting_evidence"
    resumed = bool(state.get("resumed")) or any(m.startswith("intake: reused, with") for m in messages)
    loop = None
    if waiting:
        loop = {"state": "now", "detail": "Evidence requested: " + (", ".join(evidence) or
                ", ".join((state.get("intake") or {}).get("missing_fields") or []) or "details")
                + ". The claim is paused and resumes from intake when it arrives."}
        current = "loop"
    elif resumed:
        applied = state.get("evidence_applied") or []
        loop = {"state": "done", "detail": "Evidence arrived and was checked"
                + (f" ({', '.join(map(str, applied))})" if applied else "")
                + "; the claim resumed from intake."}
    return {"stages": [{"key": k, "title": t, "state": stages[k][0], "detail": stages[k][1],
                        "label": STAGE_STATE[stages[k][0]][0], "tone": STAGE_STATE[stages[k][0]][1],
                        "current": k == current} for k, t in STAGES],
            "loop": loop, "current": current, "recorded": bool(log)}
