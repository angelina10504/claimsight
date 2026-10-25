"""
Deterministic tools for the Assessment crew.

Each check is a plain Python function (so it can be tested without an LLM) and
a thin CrewAI BaseTool wrapper around it (so an agent can call it). The agents
decide what to check and how to weigh the results; the tools only report facts.

  check_policy_period   - was the incident inside the policy's cover dates?
  check_part_prices     - is any line billed far above the market rate?
  find_prior_claims     - has this vehicle already claimed for the same parts?
  calculate_settlement  - the payout for the lines the agents admit
"""

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional, Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from settlement import calculate_settlement, explain   # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
POLICIES = ROOT / "data" / "policies"
CLAIMS = ROOT / "data" / "claims"

# Reference market prices for replacement parts (Rs, before GST). A real
# insurer would pull these from a parts catalogue; here they match the
# generator's catalogue so the check is fair to genuine bills.
MARKET_PRICES = {
    "front bumper": 9000, "rear bumper": 8500, "headlight": 7200,
    "taillight": 4300, "side mirror": 3100, "grille": 2800,
    "bonnet": 12500, "front door": 14000, "rear door": 13200,
    "fender": 6800, "windshield": 11000, "door glass": 4800,
}
OVERPRICED_RATIO = 1.5      # a part billed at 1.5x market or more is flagged
PAINT_MAX_SHARE = 0.45      # painting above 45% of parts cost is unusual
LABOUR_MAX_SHARE = 0.20     # labour above 20% of parts + painting is unusual
PRIOR_CLAIM_WINDOW_DAYS = 180


def _load_policy(policy_no: str) -> Optional[dict]:
    path = POLICIES / f"{policy_no}.json"
    return json.loads(path.read_text()) if path.exists() else None


ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _as_date(value, what: str = "date"):
    """Parse an ISO date, or say why it could not be parsed.

    Returns (date, None) or (None, {"error": ...}). A claim can legitimately
    reach these tools with no incident date yet: that is an incomplete file,
    which must be reported as such and NOT be allowed to look like an
    exclusion or a fraud signal. Raising here would stop the whole run on a
    claim that is merely waiting for a document."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, {"error": f"no {what} was supplied",
                      "reason": "incomplete_document", what: value}
    if isinstance(value, date):
        return value, None
    # The contract is a YYYY-MM-DD string. Checking the shape first is
    # deliberate: date.fromisoformat also accepts the compact form, so the
    # integer 20260530 would quietly parse as a date and hide whatever
    # upstream mistake produced a number instead of a date.
    bad = {"error": f"{what} must be an ISO date string (YYYY-MM-DD), got {value!r}",
           "reason": "invalid_input", what: str(value)}
    if not isinstance(value, str) or not ISO_DATE.match(value.strip()):
        return None, bad
    try:
        return date.fromisoformat(value.strip()), None
    except ValueError:
        return None, bad


def _part_name(description: str) -> Optional[str]:
    text = description.lower()
    for part in sorted(MARKET_PRICES, key=len, reverse=True):   # "door glass" before "door"
        if part in text:
            return part
    return None


# ------------------------------------------------------------- the checks
def check_policy_period(policy_no: str, incident_date: str) -> dict:
    """Was the incident inside the cover dates?

    Every answer is a dict. A missing or malformed date, or a policy that is
    not on file, comes back as a structured error rather than an exception, and
    never as "outside the period": not knowing the date is not the same as
    knowing the claim is out of cover."""
    policy = _load_policy(policy_no)
    if policy is None:
        return {"error": f"no policy on file for {policy_no}",
                "reason": "missing_policy", "policy_no": policy_no}
    incident, problem = _as_date(incident_date, "incident_date")
    if problem:
        return {**problem, "policy_no": policy_no}
    start, bad_start = _as_date(policy.get("policy_start"), "policy_start")
    end, bad_end = _as_date(policy.get("policy_end"), "policy_end")
    if bad_start or bad_end:
        return {"error": f"the policy schedule for {policy_no} has no usable cover dates",
                "reason": "invalid_policy_record",
                "policy_start": policy.get("policy_start"),
                "policy_end": policy.get("policy_end")}
    inside = start <= incident <= end
    return {
        "policy_no": policy_no,
        "policy_start": policy["policy_start"],
        "policy_end": policy["policy_end"],
        "incident_date": str(incident),
        "within_period": inside,
        "note": "incident inside the cover period" if inside else
                (f"incident is {(start - incident).days} days BEFORE cover began"
                 if incident < start else
                 f"incident is {(incident - end).days} days AFTER cover ended"),
    }


def check_part_prices(line_items: List[dict]) -> dict:
    """Is any line billed far above the market rate?

    An invoice with no lines is an incomplete file, not a clean bill, so it
    comes back as a structured error. Reporting suspicious=False for nothing
    at all would let an empty invoice pass a fraud check."""
    if not line_items:
        return {"error": "the invoice has no line items to check",
                "reason": "incomplete_document", "lines": [], "flags": []}
    flags, parts_total, paint, labour = [], 0.0, 0.0, 0.0
    lines = []
    for item in line_items:
        desc, amount = item["description"], float(item["amount"])
        low = desc.lower()
        part = _part_name(desc)
        if "paint" in low:
            paint += amount
            lines.append({"description": desc, "amount": amount, "check": "painting (share checked below)"})
        elif "labour" in low or "labor" in low:
            labour += amount
            lines.append({"description": desc, "amount": amount, "check": "labour (share checked below)"})
        elif part:
            parts_total += amount
            ratio = amount / MARKET_PRICES[part]
            status = "OVERPRICED" if ratio >= OVERPRICED_RATIO else "ok"
            lines.append({"description": desc, "amount": amount, "market_price": MARKET_PRICES[part],
                          "ratio": round(ratio, 2), "check": status})
            if status == "OVERPRICED":
                flags.append(f"{desc}: billed Rs {amount:,.0f}, about {ratio:.1f}x the "
                             f"market price of Rs {MARKET_PRICES[part]:,}")
        else:
            parts_total += amount
            lines.append({"description": desc, "amount": amount, "check": "no reference price"})

    if parts_total and paint / parts_total > PAINT_MAX_SHARE:
        flags.append(f"painting is {paint / parts_total:.0%} of parts cost (usual ceiling {PAINT_MAX_SHARE:.0%})")
    if parts_total + paint and labour / (parts_total + paint) > LABOUR_MAX_SHARE:
        flags.append(f"labour is {labour / (parts_total + paint):.0%} of parts and painting "
                     f"(usual ceiling {LABOUR_MAX_SHARE:.0%})")

    return {"lines": lines, "flags": flags, "suspicious": bool(flags)}


def find_prior_claims(policy_no: str, claim_id: str, incident_date: str,
                      damaged_parts: List[str]) -> dict:
    """Earlier claims on the same policy. Reads only what a claims system would
    hold (dates and billed lines), never the dataset's ground-truth labels.
    Claim ids are issued in filing order, so only lower ids count as earlier;
    this stops an original claim being flagged as a duplicate of its copy."""
    current, problem = _as_date(incident_date, "incident_date")
    if problem:
        return {**problem, "policy_no": policy_no, "claim_id": claim_id}
    wanted = {p.lower().strip() for p in (damaged_parts or [])}
    matches = []
    skipped = []
    for path in sorted(CLAIMS.glob("CLM-*.json")):
        try:
            other = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            skipped.append(path.stem)
            continue
        if other.get("claim_id", "") >= claim_id or other.get("policy_no") != policy_no:
            continue
        # Another claim with no usable date must not stop this one being
        # checked: skip it and say so, rather than raising.
        other_date, other_problem = _as_date(other.get("incident_date"))
        if other_problem:
            skipped.append(other.get("claim_id", path.stem))
            continue
        gap = (current - other_date).days
        if not 0 <= gap <= PRIOR_CLAIM_WINDOW_DAYS:
            continue
        other_parts = {_part_name(i["description"]) for i in other["line_items"]} - {None}
        overlap = sorted(wanted & other_parts)
        matches.append({"claim_id": other["claim_id"], "incident_date": other["incident_date"],
                        "days_before": gap, "overlapping_parts": overlap})

    repeat = [m for m in matches if m["overlapping_parts"]]
    return {
        "prior_claims": matches,
        "skipped_claims": skipped,          # on file but with no usable date
        "possible_duplicate": bool(repeat),
        "note": (f"same parts already claimed under {repeat[0]['claim_id']} "
                 f"{repeat[0]['days_before']} days earlier") if repeat else
                "no earlier claim on this policy for the same parts",
    }


def settle(policy_no: str, line_items: List[dict]) -> dict:
    policy = _load_policy(policy_no)
    if policy is None:
        return {"error": f"no policy on file for {policy_no}"}
    result = calculate_settlement(line_items, policy)
    return {**result.to_dict(), "explanation": explain(result)}


# ------------------------------------------------------- CrewAI wrappers
class PolicyPeriodInput(BaseModel):
    policy_no: str = Field(..., description="Policy number, e.g. POL-3000")
    incident_date: str = Field(..., description="Incident date as YYYY-MM-DD")


class PriceLine(BaseModel):
    description: str = Field(..., description="Invoice line as printed")
    amount: float = Field(..., description="Rupee amount for the line, before GST")


class PriceCheckInput(BaseModel):
    line_items: List[PriceLine] = Field(..., description="Every line on the invoice")


class PriorClaimsInput(BaseModel):
    policy_no: str = Field(..., description="Policy number, e.g. POL-3000")
    claim_id: str = Field(..., description="The current claim's id, e.g. CLM-5008")
    incident_date: str = Field(..., description="Current incident date as YYYY-MM-DD")
    damaged_parts: List[str] = Field(..., description="Parts claimed now, e.g. ['rear bumper']")


class SettlementLine(BaseModel):
    description: str = Field(..., description="Invoice line as printed")
    amount: float = Field(..., description="Rupee amount for the line, before GST")
    material: Optional[str] = Field(None, description="plastic, metal, glass, fibreglass, tyre, battery, paint or labour")
    admitted: bool = Field(True, description="False if this line should not be paid")
    reason: str = Field("", description="Why the line is not admitted, if it isn't")


class SettlementInput(BaseModel):
    policy_no: str = Field(..., description="Policy number, e.g. POL-3000")
    line_items: List[SettlementLine] = Field(..., description="Every invoice line, each marked admitted or not")


class PolicyPeriodTool(BaseTool):
    name: str = "check_policy_period"
    description: str = ("Check whether the incident date falls inside the policy's cover period. "
                        "Claims for incidents outside the period are not covered.")
    args_schema: Type[BaseModel] = PolicyPeriodInput

    def _run(self, policy_no: str, incident_date: str) -> str:
        return json.dumps(check_policy_period(policy_no, incident_date), indent=2)


class PriceCheckTool(BaseTool):
    name: str = "check_part_prices"
    description: str = ("Compare each billed part with its market price and check that painting "
                        "and labour are in a normal proportion. Returns any lines that look inflated.")
    args_schema: Type[BaseModel] = PriceCheckInput

    def _run(self, line_items: list) -> str:
        items = [i if isinstance(i, dict) else i.model_dump() for i in line_items]
        return json.dumps(check_part_prices(items), indent=2)


class PriorClaimsTool(BaseTool):
    name: str = "find_prior_claims"
    description: str = ("Look up earlier claims on the same policy in the last 180 days and report "
                        "any that billed the same parts, which suggests a duplicate claim.")
    args_schema: Type[BaseModel] = PriorClaimsInput

    def _run(self, policy_no: str, claim_id: str, incident_date: str, damaged_parts: list) -> str:
        return json.dumps(find_prior_claims(policy_no, claim_id, incident_date, damaged_parts), indent=2)


class SettlementTool(BaseTool):
    name: str = "calculate_settlement"
    description: str = ("Compute the payout under the policy's depreciation, GST, deductible and IDV "
                        "rules. Pass EVERY invoice line, setting admitted=false (with a reason) on lines "
                        "that should not be paid. Always use this tool for amounts; never calculate them yourself.")
    args_schema: Type[BaseModel] = SettlementInput

    def _run(self, policy_no: str, line_items: list) -> str:
        items = [i if isinstance(i, dict) else i.model_dump() for i in line_items]
        return json.dumps(settle(policy_no, items), indent=2)
