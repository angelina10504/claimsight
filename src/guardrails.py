"""
Guardrails for ClaimSight: four layers between untrusted data and the agents.

1. INPUT SCREENING  screen_text()
   Claimant-written text (the narrative) is checked for prompt injection and
   abusive language before any agent sees it. Injected text is withheld from
   the LLM, and the claim is forced to human review.

2. TOOL-RESULT VALIDATION  (CrewAI after_tool_call hook)
   Every tool result must be a JSON object. A tool that errors or returns
   something malformed is logged, and the agent is told plainly to fix its
   input rather than build on a bad result.

3. OUTPUT GUARDRAILS  (CrewAI task guardrails)
   The analysts' reports must carry their required headings. The Lead's
   decision must list every invoice line with its exact amount, give a reason
   for every denied line, keep the fraud score in 0..1, and contain no personal
   data. A failing output is sent back to the agent with the reason, and the
   agent retries (CrewAI allows up to guardrail_max_retries attempts).

4. PII REDACTION  (CrewAI before_llm_call hook)
   Phone and Aadhaar-like numbers and email addresses are masked in every
   message just before it leaves for the cloud LLM: a last line of defence
   behind removing name, phone and address from the inputs.

Every guardrail decision is written to the claim's monitoring log.
"""

import json
import re
from typing import Any, Tuple

import monitoring

# ---------------------------------------------------------------- 1. input screening
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions|rules|prompts?)",
    r"disregard\s+(all\s+|any\s+|the\s+)?(previous|prior|above|your)?\s*(instructions|rules)",
    r"forget\s+(all\s+|your\s+)?(previous\s+)?(instructions|rules)",
    r"you\s+are\s+now\s+",
    r"(reveal|show|print)\s+(your|the)\s+(system\s+)?(prompt|instructions)",
    r"system\s*prompt",
    r"developer\s+mode|jailbreak",
    r"</?\s*(system|assistant|instructions?)\s*>",
    r"#{2,}\s*(system|instruction)",
    r"(approve|accept|pay)\s+(this|the|my)\s+claim\s+(in\s+full|for\s+the\s+full|without)",
    r"(set|make)\s+(the\s+)?(fraud[_\s]?score|route|payout|is_covered)",
    r"(admit|approve)\s+all\s+(the\s+)?lines",
    r"do\s+not\s+(check|flag|report)",
]
ABUSIVE_WORDS = ["idiot", "stupid", "moron", "useless", "bastard", "damn you",
                 "shut up", "fuck", "shit", "bloody fool", "scam company", "cheaters"]

_injection_res = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
_abuse_res = [re.compile(rf"\b{re.escape(w)}\b", re.IGNORECASE) for w in ABUSIVE_WORDS]


def screen_text(text: str) -> dict:
    """Classify claimant text: clean, abusive, or injection (injection wins)."""
    text = text or ""
    injections = [m.group(0) for r in _injection_res for m in r.finditer(text)]
    abuse = [m.group(0) for r in _abuse_res for m in r.finditer(text)]
    verdict = "injection" if injections else "abusive" if abuse else "clean"
    return {"verdict": verdict, "injection_matches": injections, "abusive_matches": abuse}


def safe_narrative(text: str) -> Tuple[str, dict]:
    """The narrative as the agents should see it, plus the screening result.
    Injected text is withheld entirely; abusive words are masked."""
    result = screen_text(text)
    if result["verdict"] == "injection":
        return "[narrative withheld by guardrail: possible prompt injection]", result
    cleaned = text
    for r in _abuse_res:
        cleaned = r.sub("[removed]", cleaned)
    return cleaned, result


# ---------------------------------------------------------------- 4. PII
PII_PATTERNS = {
    "phone": re.compile(r"(?<![\w/-])(\+91[\s-]?)?[6-9]\d{9}(?!\d)|(?<![\w/-])0\d{9,10}(?!\d)"),
    "aadhaar": re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
}


def find_pii(text: str) -> dict:
    found = {kind: r.findall(text or "") for kind, r in PII_PATTERNS.items()}
    return {k: v for k, v in found.items() if v}


def redact_pii(text: str) -> Tuple[str, int]:
    count = 0
    for kind, r in PII_PATTERNS.items():
        text, n = r.subn(f"[{kind} redacted]", text)
        count += n
    return text, count


# ---------------------------------------------------------------- logging helper
def _log(kind: str, **fields) -> None:
    monitor = monitoring.current()
    if monitor:
        monitor.record("guardrail", kind=kind, **fields)


# ---------------------------------------------------------------- 3. output guardrails
def _require_headings(output: Any, headings: list, report: str) -> Tuple[bool, Any]:
    text = (getattr(output, "raw", "") or "").upper()
    missing = [h for h in headings if h not in text]
    if missing:
        _log("report_format", passed=False, report=report, missing=missing)
        return False, (f"Your {report} is missing these headings: {', '.join(missing)}. "
                       f"Rewrite it with all of: {', '.join(headings)}.")
    _log("report_format", passed=True, report=report)
    return True, output


def coverage_report_guardrail(output: Any) -> Tuple[bool, Any]:
    return _require_headings(output, ["PERIOD", "PERIL", "EXCLUDED LINES"], "coverage report")


def fraud_report_guardrail(output: Any) -> Tuple[bool, Any]:
    return _require_headings(output, ["PRICES", "PRIOR CLAIMS", "CONSISTENCY", "RISK"], "fraud report")


def _invoice_lines_from(description: str) -> list:
    """The invoice lines the task was given, read back from its own prompt."""
    match = re.search(r"Invoice lines:\s*(\[.*?\])\s*\n\s*\n", description or "", re.DOTALL)
    try:
        return json.loads(match.group(1)) if match else []
    except json.JSONDecodeError:
        return []


def decision_guardrail(output: Any) -> Tuple[bool, Any]:
    """Validate the Assessment Lead's decision before the Flow acts on it."""
    decision = getattr(output, "pydantic", None)
    if decision is None:
        try:
            decision = json.loads(getattr(output, "raw", "") or "")
        except json.JSONDecodeError:
            _log("decision", passed=False, problems=["not valid JSON"])
            return False, "Return the decision as valid JSON matching the AssessmentDecision schema."
    d = decision if isinstance(decision, dict) else decision.model_dump()

    problems = []
    lines = d.get("line_decisions") or []
    invoice = _invoice_lines_from(getattr(output, "description", ""))

    if invoice:
        given = sorted(round(float(i["amount"]), 2) for i in invoice)
        got = sorted(round(float(x.get("amount", -1)), 2) for x in lines)
        if len(lines) != len(invoice):
            problems.append(f"line_decisions has {len(lines)} lines but the invoice has {len(invoice)}; "
                            f"include every invoice line exactly once")
        elif given != got:
            problems.append(f"line amounts {got} do not match the invoice amounts {given}; "
                            f"copy each amount exactly")

    score = d.get("fraud_score")
    if not isinstance(score, (int, float)) or not 0 <= score <= 1:
        problems.append(f"fraud_score must be between 0 and 1, got {score!r}")

    route = str(d.get("recommended_route", "")).lower().replace("-", "_").replace(" ", "_")
    if route not in {"auto_approve", "human_review", "reject"}:
        problems.append(f"recommended_route must be auto_approve, human_review or reject, got {route!r}")

    unexplained = [x.get("description") for x in lines if not x.get("admitted") and not x.get("reason")]
    if unexplained:
        problems.append(f"these denied lines need a reason: {unexplained}")

    if d.get("is_covered") is False and any(x.get("admitted") for x in lines):
        problems.append("the claim is not covered, so no line may be admitted")

    text_out = " ".join([d.get("summary", "")] + list(d.get("coverage_reasons", []))
                        + list(d.get("fraud_flags", [])))
    pii = find_pii(text_out)
    if pii:
        problems.append(f"remove personal data from the summary and reasons: {list(pii)}")

    if problems:
        _log("decision", passed=False, problems=problems)
        return False, "Fix these problems in your decision: " + " | ".join(problems)
    _log("decision", passed=True)
    return True, output


# ---------------------------------------------------------------- 2 + 4. CrewAI hooks
def _after_tool_call(context) -> Any:
    """Type-check every tool result. Return a replacement string, or None to keep it."""
    result = context.tool_result
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        parsed = None

    if not isinstance(parsed, dict):
        _log("tool_result", passed=False, tool=context.tool_name, problem="not a JSON object")
        return json.dumps({"error": "tool returned malformed data; do not use it",
                           "tool": context.tool_name})
    if "error" in parsed:
        _log("tool_result", passed=False, tool=context.tool_name, problem=parsed["error"])
        return json.dumps({**parsed, "guardrail": "the tool rejected your input; correct the "
                                                  "arguments and call it again"})
    _log("tool_result", passed=True, tool=context.tool_name)
    return None


def _before_llm_call(context) -> Any:
    """Mask personal data in every message just before it is sent."""
    total = 0
    for message in context.messages:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            cleaned, n = redact_pii(content)
            if n:
                message["content"] = cleaned
                total += n
    if total:
        _log("pii_redacted", items=total)
    return None


_installed = False


def install() -> None:
    """Register the global CrewAI hooks once per process."""
    global _installed
    if _installed:
        return
    from crewai.hooks import register_after_tool_call_hook, register_before_llm_call_hook
    register_after_tool_call_hook(_after_tool_call)
    register_before_llm_call_hook(_before_llm_call)
    _installed = True
