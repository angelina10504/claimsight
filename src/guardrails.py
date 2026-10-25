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
   for every denied line, keep the fraud score in 0..1, state a coverage_status
   it can support, and contain no personal data. A failing output is sent back to the agent with the reason, and the
   agent retries (CrewAI allows up to guardrail_max_retries attempts).

4. PII REDACTION  (document cleaning, then a CrewAI before_llm_call hook)
   Documents are cleaned before any agent reads them: the claimant's name,
   phone and address are taken from the policy schedule and removed from the
   invoice text, along with labelled fields, phone/email/Aadhaar patterns and
   anything that looks like a postal address. The claim is stopped rather than
   sent if its own details survive. The hook then masks the patterns again in
   every outgoing message, as a last line of defence.

Every guardrail decision is written to the claim's monitoring log.
"""

import json
import re
from typing import Any, List, Optional, Tuple

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
    # Non-capturing on purpose: with a capturing group, findall returns the
    # group rather than the match, so find_pii reported "+91" or an empty
    # string instead of the number it had found.
    "phone": re.compile(r"(?<![\w/-])(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)|(?<![\w/-])0\d{9,10}(?!\d)"),
    "aadhaar": re.compile(r"(?<!\d)\d{4}\s\d{4}\s\d{4}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
}


# ---- Documents: the claimant's name and address
# A garage invoice carries the policyholder's name, and addresses and phone
# numbers for the garage. None of it is needed to assess a claim, and all of it
# would otherwise be sent to a cloud model.
#
# Names are not detected by pattern: general name-spotting is unreliable, and a
# wrong guess either leaks a name or mangles "Maruti Swift". Instead the
# claimant's own details are taken from the policy schedule, where ClaimSight
# already holds them, and those exact values are removed. That is the layer
# that actually protects the claimant; the patterns below are a net for
# whatever else a document happens to carry.

NAME_PART_MIN = 3          # ignore initials and particles like "de"
ADDRESS_PART_MIN = 10      # whole parts of an address, not single words: a city
                           # name on its own identifies nobody, and removing it
                           # would also strip the garage's address for no gain

# The markers must not contain a word the label rule below treats as a label,
# or a second pass would read "[address withheld]" as a labelled field and eat
# the rest of the line.
MARKERS = {"insured_name": "[withheld-name]", "phone": "[withheld-contact]",
           "address": "[withheld-postal]"}

# "Customer   Ravi Kumar   Vehicle   Maruti Swift" on one extracted line: take
# the value between the label and whatever label comes next.
LABELLED_FIELD = re.compile(
    r"(?i)\b(customer|insured(?:\s+name)?|client|address|phone|ph|mobile|gstin|gst\s*no|"
    r"email|e-?mail|pan)\b\s*[:.\-]?\s*"
    r"(.+?)(?=\s*\b(?:vehicle|reg\.?\s*no\.?|registration|job\s*card|date|invoice|"
    r"gstin|ph|phone|subtotal|total|description|amount|make|model)\b|$)")

# A postal address: a six-digit PIN code, with either several commas or a word
# that only appears in addresses. Money lines are excluded, so a six-digit
# amount is never mistaken for a PIN.
ADDRESS_WORD = re.compile(r"(?i)\b(road|rd|street|st|sector|nagar|colony|marg|lane|"
                          r"block|floor|phase|extn|extension|po|dist|pin|pincode)\b")
MONEY_WORD = re.compile(r"(?i)(\brs\b|₹|\bamount\b|\bsubtotal\b|\btotal\b|\bgst\s*@)")
PIN_CODE = re.compile(r"\b\d{6}\b")


class PrivacyError(RuntimeError):
    """A document still carried the claimant's own details after cleaning.

    Raised rather than handled quietly: failing a claim is recoverable, sending
    someone's name to a cloud model is not."""


def known_values(policy: Optional[dict]) -> dict:
    """The claimant's identifying details, as the policy schedule records them."""
    if not policy:
        return {}
    return {field: str(policy[field]) for field in ("insured_name", "phone", "address")
            if policy.get(field)}


def _pieces(value: str, minimum: int, by_word: bool = False) -> List[str]:
    """A value and the parts of it worth removing on their own, longest first,
    so 'Ravi Kumar' goes before 'Kumar' and no fragment is left behind.

    by_word is for names, where a surname on its own identifies someone. An
    address is split on commas only, so a bare city name is left alone."""
    parts = [p.strip(" ,.-") for p in re.split(r"[,\n]| {2,}", value)]
    if by_word:
        parts += [w.strip(" ,.-") for w in value.split()]
    out = {value.strip()} | {p for p in parts if len(p) >= minimum}
    return sorted((p for p in out if p), key=len, reverse=True)


def sanitise_document(text: str, policy: Optional[dict] = None) -> Tuple[str, dict]:
    """Remove personal data from a document before any model reads it.

    Four layers, strongest first:
      1. the claimant's own name, phone and address, taken from the policy
      2. labelled fields on the document itself (Customer, Address, Ph, GSTIN)
      3. the phone, email and Aadhaar patterns
      4. a line that looks like a postal address

    Returns the cleaned text and a report of what was removed, which goes into
    the claim's log so the redaction itself is on record."""
    if not text:
        return text, {}
    report: dict = {}

    # Labels first: a value sitting behind "Customer" or "Ph" on the document
    # itself is the most reliable thing to remove.
    text, n = LABELLED_FIELD.subn(lambda m: f"{m.group(1)}: [withheld]", text)
    if n:
        report["labelled_fields"] = n

    for field, value in known_values(policy).items():
        is_name = field == "insured_name"
        removed = 0
        for piece in _pieces(value, NAME_PART_MIN if is_name else ADDRESS_PART_MIN,
                             by_word=is_name):
            text, n = re.subn(re.escape(piece), MARKERS[field], text,
                              flags=re.IGNORECASE)
            removed += n
        if removed:
            report[f"known_{field}"] = removed

    text, n = redact_pii(text)
    if n:
        report["patterns"] = n

    lines, addresses = [], 0
    for line in text.splitlines():
        looks_postal = (PIN_CODE.search(line)
                        and (line.count(",") >= 2 or ADDRESS_WORD.search(line))
                        and not MONEY_WORD.search(line))
        if looks_postal:
            addresses += 1
            lines.append(MARKERS["address"])
        else:
            lines.append(line)
    if addresses:
        report["address_lines"] = addresses
    text = "\n".join(lines)

    _log("document_sanitised", passed=True, **report)
    return text, report


def contains_known_pii(text: str, policy: Optional[dict] = None) -> List[str]:
    """Anything of the claimant's that is still in the text. Should be empty
    after sanitise_document; if it is not, something unexpected is in the
    document and the claim must stop rather than go to a model."""
    found = []
    for field, value in known_values(policy).items():
        is_name = field == "insured_name"
        for piece in _pieces(value, NAME_PART_MIN if is_name else ADDRESS_PART_MIN,
                             by_word=is_name):
            if re.search(re.escape(piece), text or "", flags=re.IGNORECASE):
                found.append(f"{field}: {piece}")
                break
    found += [f"{kind}: {match}" for kind, matches in find_pii(text).items()
              for match in (matches if isinstance(matches, list) else [matches])]
    return found


def safe_for_cloud(text: str, policy: Optional[dict] = None,
                   what: str = "document") -> str:
    """Clean a document and refuse to pass it on if anything is left."""
    clean, _ = sanitise_document(text, policy)
    leaks = contains_known_pii(clean, policy)
    if leaks:
        _log("document_sanitised", passed=False, leaks=leaks[:3])
        raise PrivacyError(
            f"{what} still contains personal data after cleaning: {leaks[:3]}. "
            f"The claim has been stopped rather than sent to a model.")
    return clean


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

    status = str(d.get("coverage_status", "")).lower().strip()
    if status not in {"covered", "excluded", "uncertain"}:
        problems.append(f"coverage_status must be covered, excluded or uncertain, got {status!r}")
    if not (d.get("coverage_reasons") or []):
        problems.append("coverage_reasons must say what the policy wording showed, with the clause")
    if status == "excluded" and any(x.get("admitted") for x in lines):
        problems.append("coverage_status is excluded, so no line may be admitted")
    if status == "uncertain" and not (d.get("uncertainty_reasons") or []):
        problems.append("coverage_status is uncertain, so uncertainty_reasons must say what "
                        "could not be established and what would settle it")

    text_out = " ".join([d.get("summary", "")] + list(d.get("coverage_reasons", []))
                        + list(d.get("fraud_flags", [])))
    pii = find_pii(text_out)
    if pii:
        problems.append(f"remove personal data from the summary and reasons: {list(pii)}")

    # A refusal whose clause cannot be verified in the policy is recorded here
    # but NOT sent back to the agent: the router escalates such a claim to a
    # person, which is the safe outcome. Failing the task instead would spend
    # retries and could end with no decision at all.
    if status == "excluded":
        import citations
        unverified = [citations.check(r, verdict="excluded")
                      for r in (d.get("coverage_reasons") or [])]
        if not any(v["source_verified"] and v["supports_conclusion"] for v in unverified):
            _log("citation_unverified", passed=False,
                 notes=[v["note"] for v in unverified][:3],
                 note="the refusal is not backed by a clause in the policy; "
                      "the router will send this claim to a person")

    if problems:
        _log("decision", passed=False, problems=problems)
        return False, "Fix these problems in your decision: " + " | ".join(problems)
    _log("decision", passed=True)
    return True, output


# ---------------------------------------------------------------- 2 + 4. CrewAI hooks

def _after_tool_call(context) -> Any:
    """Type-check every tool result. Return a replacement string, or None to keep it.

    This hook is also where tool calls get into the monitoring log: it fires for
    every tool, including MCP ones, whereas CrewAI's step callback sees nothing
    when the model uses native tool calling. Without this the evidence report
    would show a decision with no tool results behind it."""
    result = context.tool_result
    monitor = monitoring.current()
    if monitor:
        monitor.record_tool_call(
            getattr(context, "tool_name", "unknown tool"),
            getattr(context, "tool_input", ""), result,
            agent=getattr(context, "agent", None))
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
